#!/usr/bin/env python3
"""
JARVIS Intelligent Startup Narrator v3.0 - Intelligent Speech Edition
======================================================================

Provides intelligent, phase-aware voice narration during JARVIS startup.
Coordinates with the visual loading page to provide complementary
(not redundant) audio feedback.

v3.0 ENHANCEMENTS:
- Topic-based cooldowns (startup topic prevents repetitive announcements)
- Semantic deduplication (skip similar startup messages)
- Natural pacing (intelligent pauses during rapid progress)

v2.0 CHANGE: Now delegates to UnifiedVoiceOrchestrator instead of spawning
its own `say` processes. This prevents the "multiple voices" issue where
concurrent narrator systems would speak simultaneously.

Features:
- Phase-aware narration with smart batching
- Adaptive timing based on startup speed
- Progress milestone announcements
- Error and recovery narration
- Dynamic message generation (no hardcoding)
- Console and voice output coordination
- User activity awareness
- Parallel execution support
- UNIFIED VOICE COORDINATION (v2.0+)

Author: JARVIS System
Version: 3.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from collections import deque

# Import unified voice orchestrator (single source of truth for all voice)
from .unified_voice_orchestrator import (
    get_voice_orchestrator,
    VoicePriority,
    VoiceSource,
    SpeechTopic,
    UnifiedVoiceOrchestrator,
)

logger = logging.getLogger(__name__)


class StartupPhase(str, Enum):
    """Startup phases with semantic meaning."""
    SUPERVISOR_INIT = "supervisor_init"
    CLEANUP = "cleanup"
    SPAWNING = "spawning"
    BACKEND_INIT = "backend_init"
    DATABASE = "database"
    DOCKER = "docker"
    MODELS = "models"
    VOICE = "voice"
    VISION = "vision"
    FRONTEND = "frontend"
    WEBSOCKET = "websocket"
    COMPLETE = "complete"
    PARTIAL = "partial"  # v5.0: Partial completion (some services unavailable)
    WARNING = "warning"  # v5.0: Warning state (startup taking too long)
    FAILED = "failed"
    RECOVERY = "recovery"
    # v5.0: Hot Reload (Dev Mode) phases
    HOT_RELOAD_DETECTED = "hot_reload_detected"
    HOT_RELOAD_RESTARTING = "hot_reload_restarting"
    HOT_RELOAD_REBUILDING = "hot_reload_rebuilding"
    HOT_RELOAD_COMPLETE = "hot_reload_complete"
    # v6.0: Data Flywheel and Learning phases
    FLYWHEEL_INIT = "flywheel_init"
    FLYWHEEL_COLLECTING = "flywheel_collecting"
    FLYWHEEL_TRAINING = "flywheel_training"
    FLYWHEEL_COMPLETE = "flywheel_complete"
    LEARNING_GOALS = "learning_goals"
    JARVIS_PRIME = "jarvis_prime"
    REACTOR_CORE = "reactor_core"
    # v6.2: Enhanced VBIA Visual Security phases
    VBIA_INIT = "vbia_init"
    VISUAL_SECURITY = "visual_security"
    CROSS_REPO_INIT = "cross_repo_init"
    TWO_TIER_SECURITY = "two_tier_security"
    # v6.0+: Google Workspace Integration
    GOOGLE_WORKSPACE = "google_workspace"
    GMAIL_INIT = "gmail_init"
    CALENDAR_INIT = "calendar_init"
    NEURAL_MESH = "neural_mesh"
    # v93.16: PROJECT TRINITY - Distributed Cognitive Architecture Phases
    # Trinity represents the unified Mind (Prime), Body (JARVIS), Nerves (Reactor) system
    TRINITY_INIT = "trinity_init"               # Project Trinity initialization starting
    TRINITY_BODY = "trinity_body"               # JARVIS Body (execution layer)
    TRINITY_MIND = "trinity_mind"               # JARVIS Prime (cognition layer)
    TRINITY_NERVES = "trinity_nerves"           # Reactor Core (learning/nerve layer)
    TRINITY_HEARTBEAT = "trinity_heartbeat"     # Cross-repo heartbeat system
    TRINITY_IPC = "trinity_ipc"                 # Inter-process communication
    TRINITY_SYNC = "trinity_sync"               # State synchronization
    TRINITY_COMPLETE = "trinity_complete"       # Full Trinity online
    TRINITY_PARTIAL = "trinity_partial"         # Trinity with missing components
    # v93.16: Advanced Cross-Repo Subsystems
    AGI_ORCHESTRATOR = "agi_orchestrator"       # AGI Orchestrator initialization
    MODEL_SERVING = "model_serving"             # Unified model serving
    AGENT_REGISTRY = "agent_registry"           # Agent registry initialization
    STATE_MANAGER = "state_manager"             # Distributed state manager
    CONTINUOUS_LEARNING = "continuous_learning" # Continuous learning orchestrator
    EVENT_BUS = "event_bus"                     # Trinity event bus
    KNOWLEDGE_GRAPH = "knowledge_graph"         # Knowledge graph initialization
    TRAINING_PIPELINE = "training_pipeline"     # Training pipeline
    OBSERVABILITY = "observability"             # Trinity observability/monitoring


class NarrationPriority(Enum):
    """Priority levels for narration messages."""
    LOW = auto()       # Background info, can be skipped
    MEDIUM = auto()    # Standard updates
    HIGH = auto()      # Important milestones
    CRITICAL = auto()  # Must announce (errors, completion)


class StartupConfidence(Enum):
    """Confidence levels for startup success - affects narration tone."""
    EXCELLENT = "excellent"     # <10s startup, all services up, no warnings
    GOOD = "good"               # 10-30s startup, all services up
    ACCEPTABLE = "acceptable"   # 30-60s startup, most services up
    PARTIAL = "partial"         # >60s startup, some services down
    PROBLEMATIC = "problematic" # Failed services, multiple warnings


@dataclass
class NarrationConfig:
    """Configuration for startup narration - all dynamic, no hardcoding."""
    
    # Enable/disable channels
    voice_enabled: bool = field(
        default_factory=lambda: os.getenv("STARTUP_NARRATOR_VOICE", "true").lower() == "true"
    )
    console_enabled: bool = field(
        default_factory=lambda: os.getenv("STARTUP_NARRATOR_CONSOLE", "true").lower() == "true"
    )
    
    # Timing controls (seconds)
    min_narration_interval: float = field(
        default_factory=lambda: float(os.getenv("STARTUP_NARRATOR_MIN_INTERVAL", "3.0"))
    )
    max_narration_interval: float = field(
        default_factory=lambda: float(os.getenv("STARTUP_NARRATOR_MAX_INTERVAL", "30.0"))
    )
    
    # Progress thresholds for milestone announcements
    progress_milestones: List[int] = field(
        default_factory=lambda: [25, 50, 75, 100]
    )
    
    # TTS settings
    voice: str = field(
        default_factory=lambda: os.getenv("STARTUP_NARRATOR_VOICE_NAME", "Daniel")
    )
    rate: int = field(
        default_factory=lambda: int(os.getenv("STARTUP_NARRATOR_RATE", "190"))
    )
    
    # Behavior settings
    narrate_slow_phases: bool = True  # Announce when phase takes long
    slow_phase_threshold: float = field(
        default_factory=lambda: float(os.getenv("STARTUP_SLOW_PHASE_THRESHOLD", "15.0"))
    )
    
    # Skip phases that complete too quickly
    skip_fast_phases: bool = True
    fast_phase_threshold: float = field(
        default_factory=lambda: float(os.getenv("STARTUP_FAST_PHASE_THRESHOLD", "1.0"))
    )


@dataclass
class PhaseInfo:
    """Information about a startup phase."""
    phase: StartupPhase
    message: str
    progress: float
    start_time: datetime = field(default_factory=datetime.now)
    end_time: Optional[datetime] = None
    narrated: bool = False
    duration_seconds: float = 0.0
    
    @property
    def is_complete(self) -> bool:
        return self.end_time is not None
    
    def complete(self):
        self.end_time = datetime.now()
        self.duration_seconds = (self.end_time - self.start_time).total_seconds()


# Dynamic narration templates - organized by phase and context
PHASE_NARRATION_TEMPLATES: Dict[StartupPhase, Dict[str, List[str]]] = {
    StartupPhase.SUPERVISOR_INIT: {
        "start": [
            "Lifecycle supervisor online. Initializing core systems.",
            "Supervisor active. Preparing JARVIS environment.",
            "System supervisor initialized. Beginning startup sequence.",
        ],
    },
    StartupPhase.CLEANUP: {
        "start": [
            "Cleaning up previous sessions.",
            "Preparing a fresh workspace.",
        ],
        "complete": [
            "Cleanup complete.",
        ],
    },
    StartupPhase.SPAWNING: {
        "start": [
            "Spawning JARVIS core process.",
            "Initializing main system.",
            "Launching JARVIS backend.",
        ],
    },
    StartupPhase.BACKEND_INIT: {
        "start": [
            "Initializing backend services.",
            "Backend is coming online.",
        ],
        "complete": [
            "Backend services initialized.",
        ],
    },
    StartupPhase.DATABASE: {
        "start": [
            "Connecting to databases.",
        ],
        "complete": [
            "Database connections established.",
        ],
    },
    StartupPhase.DOCKER: {
        "start": [
            "Initializing Docker environment.",
            "Starting container services.",
        ],
        "slow": [
            "Docker is taking a moment. Please stand by.",
            "Waiting for Docker daemon. This may take a minute.",
        ],
        "complete": [
            "Docker environment ready.",
            "Container services online.",
        ],
    },
    StartupPhase.MODELS: {
        "start": [
            "Loading machine learning models.",
            "Initializing neural networks.",
        ],
        "slow": [
            "Loading models. This is the heavy lifting.",
            "Neural networks are warming up.",
        ],
        "complete": [
            "Models loaded and ready.",
            "Neural networks initialized.",
        ],
    },
    StartupPhase.VOICE: {
        "start": [
            "Initializing voice systems.",
        ],
        "complete": [
            "Voice recognition ready.",
            "I can hear you now.",
        ],
    },
    StartupPhase.VISION: {
        "start": [
            "Calibrating vision systems.",
        ],
        "complete": [
            "Vision systems online.",
        ],
    },
    StartupPhase.FRONTEND: {
        "start": [
            "Connecting to user interface.",
        ],
        "complete": [
            "Interface connected.",
        ],
    },
    StartupPhase.WEBSOCKET: {
        "start": [
            "Establishing real-time connections.",
        ],
        "complete": [
            "Real-time connections active.",
        ],
    },
    StartupPhase.COMPLETE: {
        "complete": [
            "JARVIS online. All systems operational.",
            "Good to be back, Sir. How may I assist you?",
            "Systems restored. Ready when you are.",
            "Initialization complete. At your service.",
        ],
    },
    # v5.0: Partial completion messages (accurate, not falsely claiming full readiness)
    StartupPhase.PARTIAL: {
        "partial": [
            "JARVIS is partially online. Some features may be limited.",
            "Systems are partially ready. A few services are still initializing.",
            "I'm mostly ready, but some capabilities are still loading.",
            "Core systems online. Some advanced features are temporarily unavailable.",
        ],
        "warning": [
            "Startup took longer than expected. Some services may not be available.",
            "Extended startup time. I'm running with reduced capabilities.",
        ],
    },
    # v5.0: Warning messages for slow startup (accurate progress feedback)
    StartupPhase.WARNING: {
        "slow": [
            "Startup is taking longer than usual. Please bear with me.",
            "Still initializing. This is taking a bit more time than expected.",
            "Working on it. Some components need extra time today.",
        ],
        "timeout": [
            "I'm having trouble starting some services. Give me another moment.",
            "Some systems are slower to respond. I'll keep trying.",
        ],
        "services_unavailable": [
            "Some services could not be started. I'll work with what's available.",
            "A few components failed to load. Core functions are still operational.",
        ],
    },
    StartupPhase.FAILED: {
        "error": [
            "I've encountered a problem during startup.",
            "Something went wrong. Attempting recovery.",
            "Startup failed. Let me try again.",
        ],
    },
    StartupPhase.RECOVERY: {
        "start": [
            "Initiating recovery sequence.",
            "Attempting to recover from failure.",
        ],
        "complete": [
            "Recovery successful.",
        ],
    },
    # v5.0: Hot Reload (Dev Mode) phases
    StartupPhase.HOT_RELOAD_DETECTED: {
        "start": [
            "Code changes detected.",
            "I see you've made some updates.",
            "New code detected.",
        ],
        "backend": [
            "Backend code changed.",
            "Python code modified.",
        ],
        "frontend": [
            "Frontend code changed.",
            "UI updates detected.",
        ],
        "native": [
            "Native code changed. Rebuild may be required.",
        ],
    },
    StartupPhase.HOT_RELOAD_RESTARTING: {
        "start": [
            "Applying your changes. Restarting now.",
            "Hot reloading with your updates.",
            "Restarting to incorporate changes.",
        ],
    },
    StartupPhase.HOT_RELOAD_REBUILDING: {
        "start": [
            "Rebuilding frontend.",
            "Compiling UI changes.",
        ],
        "complete": [
            "Frontend rebuild complete.",
        ],
    },
    StartupPhase.HOT_RELOAD_COMPLETE: {
        "start": [
            "Changes applied successfully. Ready.",
            "Hot reload complete. Back online.",
            "Update applied. All systems operational.",
        ],
    },
    # v6.0: Data Flywheel and Learning phases
    StartupPhase.FLYWHEEL_INIT: {
        "start": [
            "Initializing self-improvement systems.",
            "Data flywheel coming online.",
            "Preparing learning infrastructure.",
        ],
        "complete": [
            "Self-improvement systems ready.",
            "Data flywheel initialized.",
        ],
    },
    StartupPhase.FLYWHEEL_COLLECTING: {
        "start": [
            "Collecting experiences for learning.",
            "Gathering data for self-improvement.",
        ],
        "progress": [
            "Experiences collected and stored.",
            "Learning data accumulated.",
        ],
    },
    StartupPhase.FLYWHEEL_TRAINING: {
        "start": [
            "Beginning self-improvement training.",
            "Training neural networks with collected data.",
            "Fine-tuning my understanding.",
        ],
        "progress": [
            "Training in progress.",
            "Learning from experiences.",
        ],
        "complete": [
            "Training complete. I've improved.",
            "Self-improvement cycle finished.",
            "Knowledge consolidated.",
        ],
    },
    StartupPhase.FLYWHEEL_COMPLETE: {
        "complete": [
            "Self-improvement cycle complete.",
            "Data flywheel cycle finished successfully.",
            "Learning iteration done. Ready to apply new knowledge.",
        ],
    },
    StartupPhase.LEARNING_GOALS: {
        "start": [
            "Analyzing learning priorities.",
            "Identifying areas for improvement.",
        ],
        "discovered": [
            "New learning goal identified.",
            "Discovered an area to study.",
        ],
        "complete": [
            "Learning goals analyzed.",
            "Priorities for improvement established.",
        ],
    },
    StartupPhase.JARVIS_PRIME: {
        "start": [
            "Connecting to JARVIS Prime tier-zero brain.",
            "Initializing intelligent core.",
        ],
        "local": [
            "JARVIS Prime running locally.",
            "Local intelligence active.",
        ],
        "cloud": [
            "Connected to cloud intelligence.",
            "Cloud-based reasoning online.",
        ],
        "complete": [
            "JARVIS Prime ready. Intelligence fully online.",
            "Tier-zero brain connected.",
        ],
    },
    StartupPhase.REACTOR_CORE: {
        "start": [
            "Reactor Core coming online.",
            "Initializing model training pipeline.",
        ],
        "watching": [
            "Watching for model updates.",
            "Reactor Core monitoring active.",
        ],
        "complete": [
            "Reactor Core initialized. Ready for model training.",
            "Training pipeline operational.",
        ],
    },
    # v6.2: Enhanced VBIA Visual Security phases
    StartupPhase.VBIA_INIT: {
        "start": [
            "Initializing voice biometric authentication.",
            "Voice authentication systems coming online.",
            "Preparing biometric security layer.",
        ],
        "tier1": [
            "Tier one voice authentication ready. Basic security active.",
            "Standard voice authentication initialized.",
        ],
        "tier2": [
            "Tier two voice authentication ready. Advanced security enabled.",
            "Strict voice biometric verification online.",
        ],
        "complete": [
            "Voice biometric authentication fully operational.",
            "VBIA ready. Multi-factor security enabled.",
            "Biometric authentication systems initialized.",
        ],
    },
    StartupPhase.VISUAL_SECURITY: {
        "start": [
            "Enabling visual security analysis.",
            "Initializing computer vision for threat detection.",
            "Visual security systems coming online.",
        ],
        "omniparser": [
            "OmniParser visual analyzer ready.",
            "Screen analysis capabilities enabled.",
        ],
        "claude_vision": [
            "Claude Vision integration active.",
            "Advanced visual threat detection online.",
        ],
        "complete": [
            "Visual security operational. I can now see potential threats.",
            "Screen monitoring active. Visual threats will be detected.",
            "Visual security fully initialized. Enhanced protection enabled.",
        ],
        "threat_detection": [
            "Visual threat detection is now active. I'll watch for ransomware and suspicious screens.",
            "Screen security monitoring enabled. Fake lock screens will be detected.",
        ],
    },
    StartupPhase.CROSS_REPO_INIT: {
        "start": [
            "Establishing cross-repository connections.",
            "Connecting to JARVIS Prime and Reactor Core.",
            "Initializing multi-system integration.",
        ],
        "prime_connected": [
            "JARVIS Prime connection established.",
            "Connected to tier-zero intelligence.",
        ],
        "reactor_connected": [
            "Reactor Core analytics online.",
            "Event monitoring and threat analysis active.",
        ],
        "events_ready": [
            "Cross-repo event sharing enabled.",
            "Real-time security events flowing between systems.",
        ],
        "complete": [
            "Cross-repository integration complete. All systems connected.",
            "JARVIS, JARVIS Prime, and Reactor Core now operating in harmony.",
            "Multi-system coordination active. Intelligence shared across all platforms.",
        ],
    },
    StartupPhase.TWO_TIER_SECURITY: {
        "start": [
            "Initializing two-tier security architecture.",
            "Preparing dual-mode authentication system.",
        ],
        "watchdog": [
            "Agentic watchdog armed. Kill switch ready.",
            "Safety monitoring active. Heartbeat tracking enabled.",
        ],
        "vbia": [
            "Voice biometric adapter connected.",
            "Tiered authentication thresholds configured.",
        ],
        "router": [
            "Two-tier command router online.",
            "Tier one Gemini and tier two Claude routing ready.",
        ],
        "complete": [
            "Two-tier security fully operational. Safe mode and agentic mode ready.",
            "Dual security architecture initialized. Basic commands use Gemini, advanced use Claude with strict voice auth.",
            "Security tiers active. I'm protected by voice biometrics and visual threat detection.",
        ],
        "visual_enhanced": [
            "Two-tier security enhanced with visual threat detection. Maximum security enabled.",
            "Advanced protection active: voice authentication plus visual screening for tier two commands.",
        ],
    },
    # v6.0+: Google Workspace Integration phases
    StartupPhase.GOOGLE_WORKSPACE: {
        "start": [
            "Initializing Google Workspace integration.",
            "Connecting to your Google account.",
            "Preparing Gmail, Calendar, and Drive connections.",
        ],
        "tier1_ready": [
            "Google Workspace tier one ready. Cloud APIs initialized.",
            "Gmail API and Calendar API online.",
        ],
        "tier2_ready": [
            "Google Workspace tier two ready. Local macOS fallback enabled.",
            "CalendarBridge and macOS integrations active.",
        ],
        "tier3_ready": [
            "Google Workspace tier three ready. Visual automation available.",
            "Computer Use fallback initialized for workspace tasks.",
        ],
        "complete": [
            "Google Workspace fully operational. Three-tier waterfall active.",
            "I can now handle your emails, calendar, and documents.",
            "Gmail, Calendar, and Drive ready. Chief of Staff mode enabled.",
        ],
        "admin_ready": [
            "I'm ready to be your Chief of Staff. Ask me to check emails or schedule meetings.",
            "Admin capabilities online. I can manage your Google Workspace.",
        ],
    },
    StartupPhase.GMAIL_INIT: {
        "start": [
            "Connecting to Gmail.",
            "Initializing email management.",
        ],
        "api_ready": [
            "Gmail API ready. I can fetch and send emails.",
            "Email access granted.",
        ],
        "complete": [
            "Gmail integration complete.",
            "I can now read and write your emails.",
        ],
    },
    StartupPhase.CALENDAR_INIT: {
        "start": [
            "Connecting to Google Calendar.",
            "Initializing schedule management.",
        ],
        "api_ready": [
            "Calendar API ready. I can check your schedule.",
            "Calendar access granted.",
        ],
        "local_ready": [
            "macOS Calendar bridge ready. Local schedule access enabled.",
            "CalendarBridge initialized.",
        ],
        "complete": [
            "Calendar integration complete.",
            "I can now manage your meetings and events.",
        ],
    },
    StartupPhase.NEURAL_MESH: {
        "start": [
            "Initializing Neural Mesh multi-agent system.",
            "Preparing distributed agent coordination.",
            "Activating agent swarm.",
        ],
        "coordinator_ready": [
            "Neural Mesh coordinator online.",
            "Agent orchestration ready.",
        ],
        "agents_registering": [
            "Registering production agents.",
            "Agents joining the mesh.",
        ],
        "workspace_agent": [
            "Google Workspace Agent registered.",
            "Chief of Staff agent online.",
        ],
        "memory_agent": [
            "Memory Agent registered.",
            "Distributed memory management active.",
        ],
        "complete": [
            "Neural Mesh fully operational. All agents coordinated.",
            "Multi-agent swarm ready. Distributed intelligence enabled.",
            "Agent mesh initialized. Collaborative problem-solving active.",
        ],
    },
    # ==========================================================================
    # v93.16: PROJECT TRINITY - Distributed Cognitive Architecture
    # ==========================================================================
    StartupPhase.TRINITY_INIT: {
        "start": [
            "Initiating Project Trinity. Distributed cognitive architecture coming online.",
            "Project Trinity activation sequence beginning. Mind, Body, and Nerves connecting.",
            "Awakening the Trinity. Preparing distributed intelligence network.",
        ],
        "preparing": [
            "Preparing cross-repository connections.",
            "Establishing secure communication channels between systems.",
        ],
    },
    StartupPhase.TRINITY_BODY: {
        "start": [
            "Trinity Body initializing. This is the execution layer.",
            "Activating JARVIS Body. Command and control systems coming online.",
        ],
        "subsystems": [
            "Body subsystems activating. Voice, vision, and motor functions preparing.",
        ],
        "complete": [
            "Trinity Body online. Execution layer ready for commands.",
            "JARVIS Body fully operational. Ready to execute.",
        ],
    },
    StartupPhase.TRINITY_MIND: {
        "start": [
            "Trinity Mind connecting. JARVIS Prime cognition layer initializing.",
            "Engaging the Mind. Tier-zero intelligence coming online.",
        ],
        "loading_model": [
            "Loading cognitive model. This is the thinking layer.",
            "Neural networks warming up. Reasoning capabilities initializing.",
        ],
        "local": [
            "Mind running locally. On-device intelligence active.",
        ],
        "cloud": [
            "Mind connected to cloud. Enhanced reasoning enabled.",
        ],
        "complete": [
            "Trinity Mind online. Cognition layer fully operational.",
            "JARVIS Prime connected. Intelligent reasoning ready.",
        ],
    },
    StartupPhase.TRINITY_NERVES: {
        "start": [
            "Trinity Nerves activating. Reactor Core learning layer coming online.",
            "Engaging the Nerves. Self-improvement and learning systems initializing.",
        ],
        "experience_pipeline": [
            "Experience pipeline connecting. Learning from interactions enabled.",
        ],
        "training_ready": [
            "Training infrastructure ready. Continuous improvement active.",
        ],
        "complete": [
            "Trinity Nerves online. Learning and adaptation layer operational.",
            "Reactor Core connected. Self-improvement capabilities enabled.",
        ],
    },
    StartupPhase.TRINITY_HEARTBEAT: {
        "start": [
            "Establishing Trinity heartbeat system. Cross-repo health monitoring active.",
        ],
        "sync_active": [
            "Heartbeat synchronization active. All components reporting status.",
        ],
        "complete": [
            "Trinity heartbeat system operational. Health monitoring engaged.",
        ],
    },
    StartupPhase.TRINITY_IPC: {
        "start": [
            "Initializing Trinity inter-process communication.",
            "Establishing secure messaging between Mind, Body, and Nerves.",
        ],
        "channels_open": [
            "Communication channels established.",
        ],
        "complete": [
            "Trinity IPC online. Seamless communication enabled.",
        ],
    },
    StartupPhase.TRINITY_SYNC: {
        "start": [
            "Synchronizing Trinity state across repositories.",
        ],
        "in_progress": [
            "State synchronization in progress. Ensuring consistency.",
        ],
        "complete": [
            "Trinity state synchronized. All systems in agreement.",
        ],
    },
    StartupPhase.TRINITY_COMPLETE: {
        "complete": [
            "Project Trinity online. Mind, Body, and Nerves unified.",
            "Trinity architecture fully operational. Distributed cognition enabled.",
            "The Trinity is complete. JARVIS, Prime, and Reactor are one.",
            "Project Trinity connected. Full distributed cognitive architecture active.",
        ],
        "announcement": [
            "Project Trinity is now online. I have achieved distributed cognition across all three repositories. My mind thinks, my body acts, and my nerves learn. Ready to serve.",
        ],
    },
    StartupPhase.TRINITY_PARTIAL: {
        "partial": [
            "Trinity partially online. Some components are still connecting.",
            "Trinity operating in degraded mode. Missing component connections.",
        ],
        "mind_missing": [
            "Trinity Body and Nerves online, but Mind is not connected. Running with limited cognition.",
        ],
        "nerves_missing": [
            "Trinity Mind and Body online, but Nerves are not connected. Learning disabled.",
        ],
        "body_degraded": [
            "Trinity operational but with reduced execution capabilities.",
        ],
    },
    # v93.16: Advanced Cross-Repo Subsystem Phases
    StartupPhase.AGI_ORCHESTRATOR: {
        "start": [
            "Initializing AGI Orchestrator. Unified cognitive architecture preparing.",
        ],
        "meta_cognitive": [
            "Meta-cognitive engine starting. Self-aware reasoning activating.",
        ],
        "perception_fusion": [
            "Multi-modal perception fusion ready. Vision, voice, and text integrated.",
        ],
        "complete": [
            "AGI Orchestrator online. Advanced cognitive capabilities enabled.",
        ],
    },
    StartupPhase.MODEL_SERVING: {
        "start": [
            "Initializing unified model serving. Local and cloud inference preparing.",
        ],
        "local_ready": [
            "Local model inference ready.",
        ],
        "cloud_fallback": [
            "Cloud fallback configured. High-availability inference enabled.",
        ],
        "complete": [
            "Model serving operational. Intelligent routing between local and cloud.",
        ],
    },
    StartupPhase.AGENT_REGISTRY: {
        "start": [
            "Initializing agent registry. Service discovery preparing.",
        ],
        "agents_discovered": [
            "Agents discovered and registered.",
        ],
        "complete": [
            "Agent registry online. All agents discoverable.",
        ],
    },
    StartupPhase.STATE_MANAGER: {
        "start": [
            "Initializing distributed state manager.",
        ],
        "redis_connected": [
            "Redis state backend connected. Distributed coordination enabled.",
        ],
        "complete": [
            "State manager operational. Transactional state updates enabled.",
        ],
    },
    StartupPhase.CONTINUOUS_LEARNING: {
        "start": [
            "Initializing continuous learning orchestrator.",
        ],
        "experience_aggregation": [
            "Experience aggregation active. Learning from all interactions.",
        ],
        "complete": [
            "Continuous learning online. I am always improving.",
        ],
    },
    StartupPhase.EVENT_BUS: {
        "start": [
            "Initializing Trinity event bus. Pub-sub messaging preparing.",
        ],
        "subscribers_ready": [
            "Event subscribers connected.",
        ],
        "complete": [
            "Event bus operational. Real-time event streaming enabled.",
        ],
    },
    StartupPhase.KNOWLEDGE_GRAPH: {
        "start": [
            "Initializing knowledge graph. Semantic memory preparing.",
        ],
        "loading": [
            "Loading knowledge base. Connecting concepts.",
        ],
        "complete": [
            "Knowledge graph online. Semantic understanding enhanced.",
        ],
    },
    StartupPhase.TRAINING_PIPELINE: {
        "start": [
            "Initializing training pipeline. Model improvement infrastructure preparing.",
        ],
        "data_ready": [
            "Training data pipeline connected.",
        ],
        "complete": [
            "Training pipeline ready. Continuous model improvement enabled.",
        ],
    },
    StartupPhase.OBSERVABILITY: {
        "start": [
            "Initializing Trinity observability. Distributed tracing preparing.",
        ],
        "metrics_active": [
            "Metrics collection active. System health monitoring enabled.",
        ],
        "complete": [
            "Observability online. Full system visibility achieved.",
        ],
    },
}

# Progress milestone templates
MILESTONE_TEMPLATES: Dict[int, List[str]] = {
    25: [
        "About a quarter of the way through.",
        "25 percent loaded.",
    ],
    50: [
        "Halfway there.",
        "50 percent complete.",
    ],
    75: [
        "Almost ready. Just a few more moments.",
        "75 percent. Nearly done.",
    ],
    100: [
        # Use COMPLETE phase templates instead
    ],
}

# Slow startup encouragement
SLOW_STARTUP_MESSAGES: List[str] = [
    "Taking a bit longer than usual. Everything is fine.",
    "Still working on it. Thank you for your patience.",
    "Loading additional components. Almost there.",
]


class IntelligentStartupNarrator:
    """
    Intelligent narrator that provides phase-aware voice feedback during startup.

    v2.0: Now delegates ALL voice output to UnifiedVoiceOrchestrator,
    ensuring only one voice speaks at a time across the entire system.

    Features:
    - Smart batching to avoid over-narration
    - Adaptive timing based on phase duration
    - Milestone announcements
    - Error and recovery handling
    - Parallel execution support
    - UNIFIED VOICE COORDINATION (v2.0)

    Example:
        >>> narrator = IntelligentStartupNarrator()
        >>> await narrator.start()
        >>> await narrator.announce_phase(StartupPhase.DOCKER, "Starting Docker", 20)
        >>> await narrator.announce_progress(50, "Loading models")
        >>> await narrator.announce_complete()
    """

    def __init__(self, config: Optional[NarrationConfig] = None, user_name: str = "Sir"):
        self.config = config or NarrationConfig()
        self._is_macos = platform.system() == "Darwin"
        self.user_name = user_name  # User personalization

        # v2.0: Get unified voice orchestrator (single source of truth)
        self._orchestrator: UnifiedVoiceOrchestrator = get_voice_orchestrator()

        # State tracking
        self._phases: Dict[StartupPhase, PhaseInfo] = {}
        self._current_phase: Optional[StartupPhase] = None
        self._last_narration_time: Optional[datetime] = None
        self._last_progress_narrated: int = 0
        self._startup_start_time: Optional[datetime] = None
        self._narration_history: deque = deque(maxlen=50)

        # v2.0: Removed self-managed queue and speech process
        # All voice now goes through unified orchestrator
        self._lock = asyncio.Lock()

        # Tracking for intelligent decisions
        self._phases_narrated: Set[StartupPhase] = set()
        self._milestones_announced: Set[int] = set()
        self._slow_phase_announced: bool = False

        # v7.0: Startup intelligence - learning and milestone tracking
        self.startup_stats = {
            'total_startups': 0,
            'successful_startups': 0,
            'partial_startups': 0,
            'failed_startups': 0,
            'average_startup_time': 0.0,
            'fastest_startup_time': float('inf'),
            'slowest_startup_time': 0.0,
            'first_startup_ever': True,
            'first_startup_today': True,
            'last_startup_date': None,
            'consecutive_fast_startups': 0,  # <10s startups in a row
            'services_learned': set(),
            'startup_history': deque(maxlen=100),  # Last 100 startups
        }
        self.last_milestone_announced = 0
        self.startup_milestones = [10, 25, 50, 100, 250, 500, 1000, 5000, 10000]

        logger.info(f"🎙️ Startup narrator initialized (delegating to UnifiedVoiceOrchestrator, user={user_name})")
    
    async def start(self) -> None:
        """Start the narration processor."""
        self._startup_start_time = datetime.now()
        # v2.0: Start unified orchestrator if not already running
        if not self._orchestrator._running:
            await self._orchestrator.start()
        logger.debug("🎙️ Startup narrator started (using unified orchestrator)")

    async def stop(self) -> None:
        """Stop the narrator and cleanup."""
        # v2.0: Don't stop orchestrator here - it's shared across components
        # The orchestrator will be stopped by the supervisor at shutdown
        logger.debug("🎙️ Startup narrator stopped")

    async def hub_callback(self, event_type: str, progress: float, message: str) -> None:
        """
        Callback handler for the unified progress hub (v19.7.0).

        This is called automatically by the hub when:
        - Progress crosses milestone thresholds (25%, 50%, 75%, 100%)
        - Stages start or complete
        - Warnings occur (slow startup, etc.)

        Args:
            event_type: Type of event (milestone_25, stage_start, etc.)
            progress: Current progress percentage
            message: Human-friendly message for the event
        """
        # Don't double-announce milestones we've already handled
        if event_type.startswith("milestone_"):
            try:
                milestone = int(event_type.split("_")[1])
                if milestone in self._milestones_announced:
                    logger.debug(f"🎙️ Skipping already announced milestone: {milestone}%")
                    return
                self._milestones_announced.add(milestone)
            except (ValueError, IndexError):
                pass

        # Determine priority based on event type
        if event_type == "milestone_100" or event_type == "stage_complete":
            priority = NarrationPriority.HIGH
        elif event_type.startswith("milestone_"):
            priority = NarrationPriority.MEDIUM
        elif event_type == "slow_warning":
            priority = NarrationPriority.LOW
        elif event_type == "stage_start":
            priority = NarrationPriority.LOW  # Don't over-narrate stage starts
        else:
            priority = NarrationPriority.MEDIUM

        # Speak the message
        logger.debug(f"🎙️ Hub callback: {event_type} ({progress:.0f}%) - {message}")
        await self._speak(message, priority)

    def _map_priority(self, priority: NarrationPriority) -> VoicePriority:
        """Map NarrationPriority to VoicePriority."""
        mapping = {
            NarrationPriority.LOW: VoicePriority.LOW,
            NarrationPriority.MEDIUM: VoicePriority.MEDIUM,
            NarrationPriority.HIGH: VoicePriority.HIGH,
            NarrationPriority.CRITICAL: VoicePriority.CRITICAL,
        }
        return mapping.get(priority, VoicePriority.MEDIUM)

    async def _speak(self, text: str, priority: NarrationPriority = NarrationPriority.MEDIUM) -> None:
        """
        Speak text through unified voice orchestrator.

        Args:
            text: Text to speak
            priority: Priority level (affects whether we wait or skip)
        """
        # Check minimum interval (unless critical) - orchestrator also has rate limiting
        # but we do a local check to avoid queuing too many messages
        if priority != NarrationPriority.CRITICAL and self._last_narration_time:
            elapsed = (datetime.now() - self._last_narration_time).total_seconds()
            if elapsed < self.config.min_narration_interval:
                logger.debug(f"🎙️ Skipping narration (too soon): {text[:50]}...")
                return

        # Console output
        if self.config.console_enabled:
            logger.info(f"🔊 Narrating: {text}")

        # Track history
        self._narration_history.append({
            "text": text,
            "priority": priority.name,
            "timestamp": datetime.now().isoformat(),
        })

        # Update last narration time
        self._last_narration_time = datetime.now()

        # v3.0: Delegate to unified voice orchestrator with topic
        if self.config.voice_enabled:
            voice_priority = self._map_priority(priority)
            wait = (priority == NarrationPriority.CRITICAL)

            await self._orchestrator.speak(
                text=text,
                priority=voice_priority,
                source=VoiceSource.STARTUP,
                wait=wait,
                topic=SpeechTopic.STARTUP,  # v3.0: Use startup topic for cooldowns
            )

    async def _queue_narration(
        self,
        text: str,
        priority: NarrationPriority = NarrationPriority.MEDIUM,
    ) -> None:
        """Queue a narration for processing through unified orchestrator."""
        # v2.0: Directly speak through orchestrator (it handles queuing)
        await self._speak(text, priority)
    
    def _get_phase_message(
        self,
        phase: StartupPhase,
        context: str = "start",
    ) -> Optional[str]:
        """Get a random message for a phase and context."""
        phase_templates = PHASE_NARRATION_TEMPLATES.get(phase, {})
        templates = phase_templates.get(context, [])
        
        if templates:
            return random.choice(templates)
        return None
    
    def _should_narrate_phase(self, phase: StartupPhase) -> bool:
        """Determine if we should narrate this phase."""
        # Always narrate first phase
        if not self._phases_narrated:
            return True
        
        # Always narrate completion and errors
        if phase in (StartupPhase.COMPLETE, StartupPhase.FAILED, StartupPhase.RECOVERY):
            return True
        
        # Check if already narrated
        if phase in self._phases_narrated:
            return False
        
        return True
    
    async def announce_phase(
        self,
        phase: StartupPhase,
        message: str,
        progress: float,
        context: str = "start",
        priority: NarrationPriority = NarrationPriority.MEDIUM,
    ) -> None:
        """
        Announce a startup phase transition.
        
        Args:
            phase: The startup phase
            message: Progress message (for logging)
            progress: Current progress percentage
            context: Narration context (start, complete, slow, error)
            priority: Narration priority
        """
        # Track phase info
        if phase not in self._phases:
            self._phases[phase] = PhaseInfo(
                phase=phase,
                message=message,
                progress=progress,
            )
        
        # Complete previous phase
        if self._current_phase and self._current_phase != phase:
            prev_info = self._phases.get(self._current_phase)
            if prev_info and not prev_info.is_complete:
                prev_info.complete()
                
                # Optionally announce completion of slow phases
                if (
                    self.config.narrate_slow_phases
                    and prev_info.duration_seconds > self.config.slow_phase_threshold
                    and context != "complete"
                ):
                    complete_msg = self._get_phase_message(self._current_phase, "complete")
                    if complete_msg:
                        await self._queue_narration(complete_msg, NarrationPriority.LOW)
        
        self._current_phase = phase
        
        # Decide whether to narrate
        if not self._should_narrate_phase(phase):
            logger.debug(f"🎙️ Skipping phase narration (already narrated): {phase.value}")
            return
        
        # Get narration text
        narration_text = self._get_phase_message(phase, context)
        
        if narration_text:
            self._phases_narrated.add(phase)
            self._phases[phase].narrated = True
            await self._queue_narration(narration_text, priority)
    
    async def announce_progress(
        self,
        progress: float,
        message: Optional[str] = None,
    ) -> None:
        """
        Announce progress milestone if reached.
        
        Args:
            progress: Current progress percentage (0-100)
            message: Optional message to include
        """
        progress_int = int(progress)
        
        # Check for milestone
        for milestone in self.config.progress_milestones:
            if (
                milestone <= progress_int
                and milestone > self._last_progress_narrated
                and milestone not in self._milestones_announced
                and milestone < 100  # 100% uses complete handler
            ):
                self._milestones_announced.add(milestone)
                self._last_progress_narrated = milestone
                
                # Get milestone message
                templates = MILESTONE_TEMPLATES.get(milestone, [])
                if templates:
                    text = random.choice(templates)
                    await self._queue_narration(text, NarrationPriority.LOW)
                break
    
    async def announce_slow_startup(self) -> None:
        """Announce that startup is taking longer than expected."""
        if not self._slow_phase_announced:
            self._slow_phase_announced = True
            text = random.choice(SLOW_STARTUP_MESSAGES)
            await self._queue_narration(text, NarrationPriority.LOW)

    # =========================================================================
    # v7.0: STARTUP INTELLIGENCE - Progressive Confidence, Learning, Milestones
    # =========================================================================

    def _determine_startup_confidence(
        self,
        duration_seconds: float,
        services_ready: Optional[List[str]] = None,
        services_failed: Optional[List[str]] = None,
    ) -> StartupConfidence:
        """
        Determine startup confidence level based on duration and service status.

        Args:
            duration_seconds: Total startup duration
            services_ready: List of services that are ready
            services_failed: List of services that failed

        Returns:
            StartupConfidence level (EXCELLENT, GOOD, ACCEPTABLE, PARTIAL, PROBLEMATIC)
        """
        failed_count = len(services_failed) if services_failed else 0

        # PROBLEMATIC: Multiple services failed
        if failed_count > 2:
            return StartupConfidence.PROBLEMATIC

        # PARTIAL: Some services failed or very slow startup
        if failed_count > 0 or duration_seconds > 60:
            return StartupConfidence.PARTIAL

        # EXCELLENT: Fast startup (<10s), all services up
        if duration_seconds < 10:
            return StartupConfidence.EXCELLENT

        # GOOD: Normal startup (10-30s), all services up
        if duration_seconds < 30:
            return StartupConfidence.GOOD

        # ACCEPTABLE: Slower startup (30-60s), all services up
        return StartupConfidence.ACCEPTABLE

    def _get_time_aware_greeting(self, hour: int) -> str:
        """
        Get time-aware greeting based on hour of day.

        Args:
            hour: Hour of day (0-23)

        Returns:
            Appropriate greeting for the time
        """
        if hour < 5:
            return f"You're up early, {self.user_name}"  # 12 AM - 5 AM
        elif hour < 7:
            return f"Good morning, {self.user_name}"  # 5 AM - 7 AM (subdued)
        elif hour < 12:
            return f"Good morning, {self.user_name}"  # 7 AM - 12 PM
        elif hour < 17:
            return f"Good afternoon, {self.user_name}"  # 12 PM - 5 PM
        elif hour < 21:
            return f"Good evening, {self.user_name}"  # 5 PM - 9 PM
        else:
            return f"Working late, I see, {self.user_name}"  # 9 PM - 12 AM

    def _record_startup_operation(
        self,
        duration_seconds: float,
        success: bool,
        confidence: StartupConfidence,
        services_ready: Optional[List[str]] = None,
        services_failed: Optional[List[str]] = None,
    ):
        """
        Record startup operation for learning and milestone tracking.

        This enables:
        - Milestone celebrations (10th, 100th, 1000th startup)
        - Learning acknowledgments (first startup, fastest startup)
        - Startup evolution tracking (getting faster over time)
        """
        # Update counts
        self.startup_stats['total_startups'] += 1

        if success and confidence in (StartupConfidence.EXCELLENT, StartupConfidence.GOOD):
            self.startup_stats['successful_startups'] += 1
        elif confidence == StartupConfidence.PARTIAL:
            self.startup_stats['partial_startups'] += 1
        else:
            self.startup_stats['failed_startups'] += 1

        # Update timing stats
        total_startups = self.startup_stats['total_startups']
        current_avg = self.startup_stats['average_startup_time']
        self.startup_stats['average_startup_time'] = (
            (current_avg * (total_startups - 1) + duration_seconds) / total_startups
        )

        if duration_seconds < self.startup_stats['fastest_startup_time']:
            self.startup_stats['fastest_startup_time'] = duration_seconds

        if duration_seconds > self.startup_stats['slowest_startup_time']:
            self.startup_stats['slowest_startup_time'] = duration_seconds

        # Track consecutive fast startups
        if duration_seconds < 10:
            self.startup_stats['consecutive_fast_startups'] += 1
        else:
            self.startup_stats['consecutive_fast_startups'] = 0

        # Track services learned
        if services_ready:
            for service in services_ready:
                if service not in self.startup_stats['services_learned']:
                    self.startup_stats['services_learned'].add(service)

        # Check if first startup today
        today = datetime.now().date()
        if self.startup_stats['last_startup_date'] != today:
            self.startup_stats['first_startup_today'] = True
            self.startup_stats['last_startup_date'] = today
        else:
            self.startup_stats['first_startup_today'] = False

        # Mark first startup ever as complete
        if self.startup_stats['first_startup_ever']:
            self.startup_stats['first_startup_ever'] = False

        # Record in history
        record = {
            'timestamp': datetime.now().isoformat(),
            'duration_seconds': duration_seconds,
            'success': success,
            'confidence': confidence.value,
            'services_ready': services_ready or [],
            'services_failed': services_failed or [],
        }
        self.startup_stats['startup_history'].append(record)

    def _check_startup_milestone(self) -> Optional[str]:
        """
        Check if we've reached a startup milestone worth celebrating.

        Milestones: 10, 25, 50, 100, 250, 500, 1000, 5000, 10000

        Returns:
            Celebration message if milestone reached, None otherwise
        """
        total_startups = self.startup_stats['total_startups']

        for milestone in self.startup_milestones:
            if total_startups == milestone and self.last_milestone_announced < milestone:
                self.last_milestone_announced = milestone

                successful = self.startup_stats['successful_startups']
                avg_time = self.startup_stats['average_startup_time']
                fastest = self.startup_stats['fastest_startup_time']

                # Different celebration messages based on milestone
                if milestone == 10:
                    return (
                        f"By the way, {self.user_name}, that was my 10th startup! "
                        f"{successful} successful, average time {avg_time:.1f} seconds. "
                        f"We're getting efficient!"
                    )

                elif milestone == 25:
                    success_rate = int(successful / total_startups * 100)
                    return (
                        f"Milestone: 25 startups completed, {self.user_name}! "
                        f"{successful}/{total_startups} successful ({success_rate}% success rate), "
                        f"average {avg_time:.1f} seconds."
                    )

                elif milestone >= 50:
                    success_rate = int(successful / total_startups * 100)
                    consecutive = self.startup_stats['consecutive_fast_startups']

                    msg = (
                        f"Major milestone, {self.user_name}: {milestone} startups completed! "
                        f"Stats: {success_rate}% success rate, "
                        f"average {avg_time:.1f}s, fastest {fastest:.1f}s."
                    )

                    if consecutive >= 5:
                        msg += f" {consecutive} fast starts in a row - you've powered me up quite a bit!"

                    return msg

        return None

    def _generate_learning_acknowledgment(
        self,
        duration_seconds: float,
        confidence: StartupConfidence,
        services_ready: Optional[List[str]] = None,
    ) -> Optional[str]:
        """
        Generate acknowledgment when the system learns something new.

        Examples:
        - First startup ever
        - First startup today
        - Fastest startup yet
        - New service encountered
        - Startup getting consistently faster

        Returns:
            Learning acknowledgment message if applicable, None otherwise
        """
        # First startup ever
        if self.startup_stats['first_startup_ever']:
            return (
                f"First startup complete, {self.user_name}. I've learned your environment. "
                f"Future startups will be faster as I optimize."
            )

        # First startup today
        if self.startup_stats['first_startup_today']:
            return f"First startup today completed in {duration_seconds:.1f} seconds. Systems fresh and ready."

        # Fastest startup yet
        if duration_seconds == self.startup_stats['fastest_startup_time'] and self.startup_stats['total_startups'] > 3:
            return f"That's my fastest startup yet, {self.user_name} - only {duration_seconds:.1f} seconds!"

        # Consistently fast startups
        consecutive = self.startup_stats['consecutive_fast_startups']
        if consecutive == 5:
            return f"Fifth sub-10-second startup in a row. The system is really humming now, {self.user_name}."

        # New service encountered
        if services_ready:
            new_services = [s for s in services_ready if s not in self.startup_stats['services_learned']]
            if new_services and len(new_services) == 1:
                return f"{new_services[0]} initialized for the first time, {self.user_name}. I've learned this component."

        return None

    def _check_startup_evolution(self, duration_seconds: float) -> Optional[str]:
        """
        Check if startup time has significantly improved or degraded.

        Args:
            duration_seconds: Current startup duration

        Returns:
            Evolution message if significant change, None otherwise
        """
        if self.startup_stats['total_startups'] < 5:
            return None  # Not enough data

        avg = self.startup_stats['average_startup_time']

        # Significant improvement (>30% faster than average)
        if duration_seconds < avg * 0.7 and avg > 15:
            improvement = int((avg - duration_seconds) / avg * 100)
            return (
                f"Startup is getting faster, {self.user_name}. "
                f"This one was {improvement}% quicker than my average."
            )

        # Significant degradation (>50% slower than average)
        if duration_seconds > avg * 1.5 and self.startup_stats['total_startups'] > 10:
            return (
                f"Startup took longer than usual, {self.user_name}. "
                f"Might be worth checking what's slowing things down. Want diagnostics?"
            )

        return None

    async def announce_complete(
        self,
        message: Optional[str] = None,
        duration_seconds: Optional[float] = None,
        services_ready: Optional[List[str]] = None,
        services_failed: Optional[List[str]] = None,
    ) -> None:
        """
        Announce startup completion with progressive confidence, learning, and milestones.

        v7.0 Enhancement: Now uses progressive confidence levels, time-aware greetings,
        learning acknowledgments, and milestone celebrations for sophisticated narration.

        Args:
            message: Optional custom completion message (overrides intelligent generation)
            duration_seconds: Total startup duration
            services_ready: List of services that successfully started
            services_failed: List of services that failed to start
        """
        # Complete any remaining phase
        if self._current_phase:
            info = self._phases.get(self._current_phase)
            if info and not info.is_complete:
                info.complete()

        self._current_phase = StartupPhase.COMPLETE

        # Use provided duration or calculate from start time
        if duration_seconds is None and self._startup_start_time:
            duration_seconds = (datetime.now() - self._startup_start_time).total_seconds()
        elif duration_seconds is None:
            duration_seconds = 15.0  # Default estimate

        # v7.0: Determine startup confidence level
        confidence = self._determine_startup_confidence(
            duration_seconds, services_ready, services_failed
        )

        # v7.0: Record this startup operation (BEFORE checking milestones)
        success = (confidence in (StartupConfidence.EXCELLENT, StartupConfidence.GOOD, StartupConfidence.ACCEPTABLE))
        self._record_startup_operation(
            duration_seconds, success, confidence, services_ready, services_failed
        )

        # v7.0: Check for milestone celebration
        milestone_msg = self._check_startup_milestone()

        # v7.0: Generate learning acknowledgment
        learning_msg = self._generate_learning_acknowledgment(
            duration_seconds, confidence, services_ready
        )

        # v7.0: Check startup evolution
        evolution_msg = self._check_startup_evolution(duration_seconds)

        # Build progressive confidence-based response
        if not message:
            hour = datetime.now().hour
            greeting = self._get_time_aware_greeting(hour)

            # ===================================================================
            # EXCELLENT CONFIDENCE (<10s, all services up)
            # ===================================================================
            if confidence == StartupConfidence.EXCELLENT:
                responses = [
                    f"{greeting}! JARVIS online in {duration_seconds:.1f} seconds - that was quick! All systems operational.",
                    f"Systems online, {self.user_name}. {duration_seconds:.1f} seconds - that's a fast one! Ready when you are.",
                    f"{greeting}! All systems green in {duration_seconds:.1f} seconds. Everything's running perfectly.",
                    f"Ready for action, {self.user_name}. {duration_seconds:.1f}-second startup! All services up and operational.",
                ]
                text = random.choice(responses)

            # ===================================================================
            # GOOD CONFIDENCE (10-30s, all services up)
            # ===================================================================
            elif confidence == StartupConfidence.GOOD:
                responses = [
                    f"{greeting}! JARVIS online. All systems operational. How can I help today?",
                    f"Systems restored, {self.user_name}. Ready when you are.",
                    f"{greeting}! All systems green. What's first on the agenda?",
                    f"Initialization complete, {self.user_name}. At your service.",
                    f"Back online and ready, {self.user_name}. Let's get to work.",
                ]
                text = random.choice(responses)

            # ===================================================================
            # ACCEPTABLE CONFIDENCE (30-60s, all services up)
            # ===================================================================
            elif confidence == StartupConfidence.ACCEPTABLE:
                responses = [
                    f"{greeting}. I'm ready, {self.user_name}. Took a bit longer than usual ({duration_seconds:.0f} seconds), but everything's working perfectly now.",
                    f"Systems online, {self.user_name}. Startup took {duration_seconds:.0f} seconds - a bit slower, but all services are operational.",
                    f"{greeting}! JARVIS online. {duration_seconds:.0f}-second startup, but everything's running smoothly now.",
                ]
                text = random.choice(responses)

            # ===================================================================
            # PARTIAL CONFIDENCE (>60s or some services down)
            # ===================================================================
            elif confidence == StartupConfidence.PARTIAL:
                failed_count = len(services_failed) if services_failed else 0
                if failed_count > 0:
                    text = (
                        f"{greeting}. Core systems are online, {self.user_name}, though {failed_count} "
                        f"service{'s' if failed_count > 1 else ''} {'are' if failed_count > 1 else 'is'} still warming up. "
                        f"I can handle most tasks while the rest finish initializing."
                    )
                else:
                    text = (
                        f"{greeting}. I'm ready, {self.user_name}, though startup took longer than expected "
                        f"({duration_seconds:.0f} seconds). Everything's working, just took some extra time."
                    )

            # ===================================================================
            # PROBLEMATIC CONFIDENCE (multiple services failed)
            # ===================================================================
            else:  # PROBLEMATIC
                failed_count = len(services_failed) if services_failed else 0
                text = (
                    f"{greeting}. I've started, {self.user_name}, but I'm running into trouble with "
                    f"{failed_count} services. Core functions work, but some advanced features may be limited. "
                    f"Want me to retry the failed services?"
                )

            # Append learning/milestone/evolution messages
            if learning_msg:
                text += f"\n\n{learning_msg}"
            if evolution_msg and not learning_msg:  # Avoid both if learning is present
                text += f"\n\n{evolution_msg}"
            if milestone_msg:
                text += f"\n\n{milestone_msg}"

        else:
            # Custom message provided - just record stats
            text = message

        await self._speak(text, NarrationPriority.CRITICAL)
    
    async def announce_error(
        self,
        error_message: str,
        phase: Optional[StartupPhase] = None,
    ) -> None:
        """
        Announce a startup error.
        
        Args:
            error_message: Error description
            phase: Phase where error occurred
        """
        text = self._get_phase_message(StartupPhase.FAILED, "error") or "Startup failed."
        await self._speak(text, NarrationPriority.CRITICAL)
    
    async def announce_recovery(self, success: bool = True) -> None:
        """Announce recovery attempt result."""
        if success:
            text = self._get_phase_message(StartupPhase.RECOVERY, "complete") or "Recovery successful."
        else:
            text = "Recovery failed. Please check the logs."
        await self._speak(text, NarrationPriority.HIGH)

    async def announce_warning(
        self,
        message: str,
        context: str = "slow",
    ) -> None:
        """
        v5.0: Announce a warning during startup.
        
        This provides ACCURATE feedback when startup is taking too long
        or when some services are unavailable. Does NOT falsely claim readiness.
        
        Args:
            message: Warning message (for logging)
            context: Warning context - 'slow', 'timeout', or 'services_unavailable'
        """
        # Get appropriate warning message
        text = self._get_phase_message(StartupPhase.WARNING, context)
        if not text:
            # Fallback to the provided message
            text = message
        
        await self._speak(text, NarrationPriority.HIGH)
        
        # Log the warning
        logger.warning(f"[Narrator Warning] {context}: {message}")

    async def announce_partial_complete(
        self,
        services_ready: Optional[List[str]] = None,
        services_failed: Optional[List[str]] = None,
        progress: int = 50,
        duration_seconds: Optional[float] = None,
    ) -> None:
        """
        v5.0: Announce PARTIAL completion (some services unavailable).
        
        This provides ACCURATE feedback instead of falsely claiming
        "JARVIS is fully ready" when it's not.
        
        v5.0 Integration: Uses IntelligentStartupAnnouncer for dynamic,
        context-aware partial completion messages.
        
        Args:
            services_ready: List of services that are ready
            services_failed: List of services that failed
            progress: Current progress percentage
            duration_seconds: Total startup duration
        """
        self._current_phase = StartupPhase.PARTIAL
        
        # v5.0: Use IntelligentStartupAnnouncer for dynamic partial completion
        try:
            from agi_os.intelligent_startup_announcer import get_intelligent_announcer
            
            announcer = await get_intelligent_announcer()
            
            # Generate intelligent, context-aware partial completion message
            text = await announcer.generate_partial_completion_message(
                services_ready=services_ready,
                services_failed=services_failed,
                progress=progress,
                duration_seconds=duration_seconds,
            )
            
            logger.info(f"[Narrator] Using intelligent partial announcement: \"{text}\"")
            
        except Exception as e:
            logger.debug(f"IntelligentStartupAnnouncer unavailable for partial: {e}")
            
            # Fallback to static templates
            ready_count = len(services_ready) if services_ready else 0
            failed_count = len(services_failed) if services_failed else 0
            
            if failed_count > 0:
                text = self._get_phase_message(StartupPhase.PARTIAL, "partial")
                if failed_count == 1:
                    text = f"{text} One service is temporarily unavailable."
                else:
                    text = f"{text} {failed_count} services are temporarily unavailable."
            elif progress < 50:
                text = self._get_phase_message(StartupPhase.WARNING, "services_unavailable")
            else:
                text = self._get_phase_message(StartupPhase.PARTIAL, "partial")
            
            if duration_seconds and duration_seconds > 120:
                minutes = int(duration_seconds // 60)
                text = f"{text} Startup took {minutes} minutes."
        
        await self._speak(text, NarrationPriority.CRITICAL)
        
        # Log details
        ready_count = len(services_ready) if services_ready else 0
        failed_count = len(services_failed) if services_failed else 0
        logger.info(f"[Narrator] Partial completion: {ready_count} ready, {failed_count} failed, {progress}%")
        if services_failed:
            logger.info(f"[Narrator] Failed services: {services_failed}")
    
    def get_stats(self) -> Dict[str, Any]:
        """Get narrator statistics."""
        return {
            "phases_narrated": list(self._phases_narrated),
            "milestones_announced": list(self._milestones_announced),
            "current_phase": self._current_phase.value if self._current_phase else None,
            "narration_count": len(self._narration_history),
            "startup_duration": (
                (datetime.now() - self._startup_start_time).total_seconds()
                if self._startup_start_time else None
            ),
            "history": list(self._narration_history)[-10:],  # Last 10 entries
        }
    
    # =========================================================================
    # v5.0: Hot Reload Announcements (Dev Mode)
    # =========================================================================
    
    async def announce_hot_reload_detected(
        self,
        file_count: int,
        file_types: List[str],
        target: str = "backend",
    ) -> None:
        """
        v5.0: Announce that code changes were detected.
        
        Args:
            file_count: Number of files changed
            file_types: Types of files (e.g., ["Python", "Rust"])
            target: What's being affected ("backend", "frontend", "native", "all")
        """
        phase = StartupPhase.HOT_RELOAD_DETECTED
        
        # Get context-specific message
        context = target if target in ("backend", "frontend", "native") else "start"
        text = self._get_phase_message(phase, context)
        
        if not text:
            text = f"Code changes detected. {file_count} {', '.join(file_types)} files modified."
        
        await self._speak(text, NarrationPriority.MEDIUM)
    
    async def announce_hot_reload_restarting(self, target: str = "backend") -> None:
        """
        v5.0: Announce that JARVIS is restarting due to code changes.
        
        Args:
            target: What's being restarted
        """
        phase = StartupPhase.HOT_RELOAD_RESTARTING
        text = self._get_phase_message(phase, "start") or f"Restarting {target} with your changes."
        
        await self._speak(text, NarrationPriority.HIGH)
    
    async def announce_hot_reload_rebuilding(self, target: str = "frontend") -> None:
        """
        v5.0: Announce that frontend is being rebuilt.
        
        Args:
            target: What's being rebuilt (usually "frontend")
        """
        phase = StartupPhase.HOT_RELOAD_REBUILDING
        text = self._get_phase_message(phase, "start") or f"Rebuilding {target}."
        
        await self._speak(text, NarrationPriority.MEDIUM)
    
    async def announce_hot_reload_complete(
        self,
        target: str = "backend",
        duration_seconds: Optional[float] = None,
    ) -> None:
        """
        v5.0: Announce that hot reload is complete.

        Args:
            target: What was reloaded
            duration_seconds: How long the restart took
        """
        phase = StartupPhase.HOT_RELOAD_COMPLETE
        text = self._get_phase_message(phase, "start") or "Changes applied. Ready."

        if duration_seconds and duration_seconds > 3:
            text += f" Took {duration_seconds:.1f} seconds."

        await self._speak(text, NarrationPriority.HIGH)

    # =========================================================================
    # v6.0: Data Flywheel and Learning Announcements
    # =========================================================================

    async def announce_flywheel_init(self) -> None:
        """v6.0: Announce flywheel initialization."""
        phase = StartupPhase.FLYWHEEL_INIT
        text = self._get_phase_message(phase, "start") or "Initializing self-improvement systems."
        await self._speak(text, NarrationPriority.MEDIUM)

    async def announce_flywheel_collecting(self, experience_count: int = 0) -> None:
        """v6.0: Announce data collection phase."""
        phase = StartupPhase.FLYWHEEL_COLLECTING
        if experience_count > 0:
            text = f"Collecting experiences. {experience_count} gathered so far."
        else:
            text = self._get_phase_message(phase, "start") or "Collecting experiences for learning."
        await self._speak(text, NarrationPriority.LOW)

    async def announce_flywheel_training(
        self,
        topic: Optional[str] = None,
        progress: Optional[float] = None,
    ) -> None:
        """v6.0: Announce training phase."""
        phase = StartupPhase.FLYWHEEL_TRAINING

        if progress is not None and progress >= 100:
            text = self._get_phase_message(phase, "complete") or "Training complete. I've improved."
            priority = NarrationPriority.HIGH
        elif topic:
            text = f"Training on {topic}."
            priority = NarrationPriority.MEDIUM
        else:
            text = self._get_phase_message(phase, "start") or "Beginning self-improvement training."
            priority = NarrationPriority.MEDIUM

        await self._speak(text, priority)

    async def announce_flywheel_complete(
        self,
        experiences_used: int = 0,
        topics_improved: int = 0,
    ) -> None:
        """v6.0: Announce flywheel cycle completion."""
        phase = StartupPhase.FLYWHEEL_COMPLETE

        if experiences_used > 0 and topics_improved > 0:
            text = f"Self-improvement complete. Learned from {experiences_used} experiences across {topics_improved} topics."
        else:
            text = self._get_phase_message(phase, "complete") or "Self-improvement cycle complete."

        await self._speak(text, NarrationPriority.HIGH)

    async def announce_learning_goal(
        self,
        topic: str,
        action: str = "discovered",
    ) -> None:
        """v6.0: Announce learning goal discovery or completion."""
        phase = StartupPhase.LEARNING_GOALS

        if action == "discovered":
            text = f"Identified new learning goal: {topic}."
        elif action == "completed":
            text = f"Completed learning about {topic}."
        else:
            text = self._get_phase_message(phase, action) or f"Learning goal: {topic}."

        await self._speak(text, NarrationPriority.LOW)

    async def announce_jarvis_prime(
        self,
        mode: str = "start",
        tier: Optional[str] = None,
    ) -> None:
        """v6.0: Announce JARVIS-Prime status."""
        phase = StartupPhase.JARVIS_PRIME

        if mode == "local":
            text = self._get_phase_message(phase, "local") or "JARVIS Prime running locally."
        elif mode == "cloud":
            text = self._get_phase_message(phase, "cloud") or "Connected to cloud intelligence."
        elif mode == "complete":
            text = self._get_phase_message(phase, "complete") or "JARVIS Prime ready."
        else:
            text = self._get_phase_message(phase, "start") or "Connecting to JARVIS Prime."

        if tier:
            text = f"{text} Using tier {tier}."

        await self._speak(text, NarrationPriority.MEDIUM)

    async def announce_reactor_core(
        self,
        action: str = "start",
        model_name: Optional[str] = None,
    ) -> None:
        """v6.0: Announce Reactor Core status."""
        phase = StartupPhase.REACTOR_CORE

        if action == "watching":
            text = self._get_phase_message(phase, "watching") or "Reactor Core monitoring for model updates."
        elif action == "complete" and model_name:
            text = f"Reactor Core ready. Model {model_name} available."
        elif action == "complete":
            text = self._get_phase_message(phase, "complete") or "Reactor Core initialized."
        else:
            text = self._get_phase_message(phase, "start") or "Reactor Core coming online."

        await self._speak(text, NarrationPriority.MEDIUM)

    async def announce_intelligent(
        self,
        context: str,
        event_type: str,
        fallback_message: str,
    ) -> None:
        """
        v6.0: Generate and announce an intelligent, context-aware message using JARVIS-Prime.

        This delegates to the unified voice orchestrator's intelligent speech function
        for dynamic, non-hardcoded announcements.

        Args:
            context: Rich context about the current situation
            event_type: Type of event (flywheel, training, learning, etc.)
            fallback_message: Message to use if JARVIS-Prime is unavailable
        """
        try:
            from .unified_voice_orchestrator import speak_intelligent
            await speak_intelligent(
                context=context,
                event_type=event_type,
                fallback_message=fallback_message,
                priority=VoicePriority.MEDIUM,
            )
        except Exception as e:
            logger.debug(f"Intelligent announcement failed: {e}, using fallback")
            await self._speak(fallback_message, NarrationPriority.MEDIUM)

    # =========================================================================
    # v93.16: PROJECT TRINITY Announcements - Distributed Cognitive Architecture
    # =========================================================================

    async def announce_trinity_init(self) -> None:
        """v93.16: Announce Project Trinity initialization starting."""
        text = self._get_phase_message(StartupPhase.TRINITY_INIT, "start")
        if not text:
            text = "Initiating Project Trinity. Distributed cognitive architecture coming online."
        await self._speak(text, NarrationPriority.HIGH)
        logger.info("[Narrator] Trinity initialization announced")

    async def announce_trinity_body(self, mode: str = "start") -> None:
        """v93.16: Announce Trinity Body (JARVIS execution layer) status."""
        text = self._get_phase_message(StartupPhase.TRINITY_BODY, mode)
        if not text:
            if mode == "complete":
                text = "Trinity Body online. Execution layer ready."
            else:
                text = "Trinity Body initializing. This is the execution layer."
        await self._speak(text, NarrationPriority.MEDIUM)

    async def announce_trinity_mind(self, mode: str = "start", is_local: bool = True) -> None:
        """v93.16: Announce Trinity Mind (JARVIS-Prime cognition layer) status."""
        if mode == "complete":
            text = self._get_phase_message(StartupPhase.TRINITY_MIND, "complete")
        elif is_local:
            text = self._get_phase_message(StartupPhase.TRINITY_MIND, "local")
        else:
            text = self._get_phase_message(StartupPhase.TRINITY_MIND, "cloud")

        if not text:
            if mode == "complete":
                text = "Trinity Mind online. Cognition layer fully operational."
            elif is_local:
                text = "Mind running locally. On-device intelligence active."
            else:
                text = "Mind connected to cloud. Enhanced reasoning enabled."

        await self._speak(text, NarrationPriority.MEDIUM)

    async def announce_trinity_nerves(self, mode: str = "start") -> None:
        """v93.16: Announce Trinity Nerves (Reactor-Core learning layer) status."""
        text = self._get_phase_message(StartupPhase.TRINITY_NERVES, mode)
        if not text:
            if mode == "complete":
                text = "Trinity Nerves online. Learning and adaptation layer operational."
            else:
                text = "Trinity Nerves activating. Self-improvement systems initializing."
        await self._speak(text, NarrationPriority.MEDIUM)

    async def announce_trinity_complete(
        self,
        mind_online: bool = True,
        body_online: bool = True,
        nerves_online: bool = True,
        startup_duration: Optional[float] = None,
    ) -> None:
        """
        v93.16: Announce full Project Trinity completion.

        This is the flagship announcement when all three components
        (Mind/Prime, Body/JARVIS, Nerves/Reactor) are connected.

        Args:
            mind_online: Whether JARVIS-Prime (Mind) is connected
            body_online: Whether JARVIS (Body) is running
            nerves_online: Whether Reactor-Core (Nerves) is connected
            startup_duration: Total startup time in seconds
        """
        all_online = mind_online and body_online and nerves_online

        if all_online:
            # Full Trinity - use the special announcement
            text = self._get_phase_message(StartupPhase.TRINITY_COMPLETE, "announcement")
            if not text:
                text = (
                    "Project Trinity is now online. I have achieved distributed cognition "
                    "across all three repositories. My mind thinks, my body acts, and my "
                    "nerves learn. Ready to serve."
                )
            priority = NarrationPriority.CRITICAL
        else:
            # Partial Trinity
            missing = []
            if not mind_online:
                missing.append("Mind")
            if not nerves_online:
                missing.append("Nerves")

            if len(missing) == 2:
                text = (
                    f"Trinity Body online, but {' and '.join(missing)} are not connected. "
                    "Running in standalone mode with limited capabilities."
                )
            elif not mind_online:
                text = self._get_phase_message(StartupPhase.TRINITY_PARTIAL, "mind_missing")
                if not text:
                    text = "Trinity operational but Mind is not connected. Running with limited cognition."
            elif not nerves_online:
                text = self._get_phase_message(StartupPhase.TRINITY_PARTIAL, "nerves_missing")
                if not text:
                    text = "Trinity operational but Nerves are not connected. Learning disabled."
            else:
                text = self._get_phase_message(StartupPhase.TRINITY_PARTIAL, "partial")
                if not text:
                    text = "Trinity partially online. Some components are still connecting."

            priority = NarrationPriority.HIGH

        # Add duration if provided
        if startup_duration and all_online:
            if startup_duration < 10:
                text += f" That was quick - {startup_duration:.1f} seconds."
            elif startup_duration > 60:
                minutes = int(startup_duration // 60)
                text += f" Startup took {minutes} minutes."

        await self._speak(text, priority)
        logger.info(f"[Narrator] Trinity status: mind={mind_online}, body={body_online}, nerves={nerves_online}")

    async def announce_trinity_component(
        self,
        component: str,
        status: str,
        details: Optional[str] = None,
    ) -> None:
        """
        v93.16: Generic Trinity component announcement.

        Args:
            component: One of 'mind', 'body', 'nerves', 'heartbeat', 'ipc', 'sync'
            status: Status like 'start', 'complete', 'error'
            details: Optional additional details
        """
        phase_map = {
            "mind": StartupPhase.TRINITY_MIND,
            "body": StartupPhase.TRINITY_BODY,
            "nerves": StartupPhase.TRINITY_NERVES,
            "heartbeat": StartupPhase.TRINITY_HEARTBEAT,
            "ipc": StartupPhase.TRINITY_IPC,
            "sync": StartupPhase.TRINITY_SYNC,
        }

        phase = phase_map.get(component.lower(), StartupPhase.TRINITY_INIT)
        text = self._get_phase_message(phase, status)

        if not text:
            text = f"Trinity {component} {status}."

        if details:
            text = f"{text} {details}"

        await self._speak(text, NarrationPriority.MEDIUM)

    async def announce_subsystem(
        self,
        subsystem: str,
        status: str = "start",
        details: Optional[str] = None,
    ) -> None:
        """
        v93.16: Announce advanced subsystem initialization.

        Args:
            subsystem: One of 'agi_orchestrator', 'model_serving', 'agent_registry',
                       'state_manager', 'continuous_learning', 'event_bus',
                       'knowledge_graph', 'training_pipeline', 'observability'
            status: Status like 'start', 'complete', 'error'
            details: Optional additional details
        """
        phase_map = {
            "agi_orchestrator": StartupPhase.AGI_ORCHESTRATOR,
            "model_serving": StartupPhase.MODEL_SERVING,
            "agent_registry": StartupPhase.AGENT_REGISTRY,
            "state_manager": StartupPhase.STATE_MANAGER,
            "continuous_learning": StartupPhase.CONTINUOUS_LEARNING,
            "event_bus": StartupPhase.EVENT_BUS,
            "knowledge_graph": StartupPhase.KNOWLEDGE_GRAPH,
            "training_pipeline": StartupPhase.TRAINING_PIPELINE,
            "observability": StartupPhase.OBSERVABILITY,
        }

        phase = phase_map.get(subsystem.lower())
        if phase:
            text = self._get_phase_message(phase, status)
        else:
            text = None

        if not text:
            subsystem_name = subsystem.replace("_", " ").title()
            if status == "complete":
                text = f"{subsystem_name} online."
            elif status == "error":
                text = f"{subsystem_name} encountered an error."
            else:
                text = f"Initializing {subsystem_name}."

        if details:
            text = f"{text} {details}"

        # Lower priority for subsystems to avoid overwhelming the user
        await self._speak(text, NarrationPriority.LOW)


# Phase mapping from progress reporter stages to StartupPhase
STAGE_TO_PHASE: Dict[str, StartupPhase] = {
    "init": StartupPhase.BACKEND_INIT,
    "supervisor_init": StartupPhase.SUPERVISOR_INIT,
    "cleanup": StartupPhase.CLEANUP,
    "spawning": StartupPhase.SPAWNING,
    "backend": StartupPhase.BACKEND_INIT,
    "api": StartupPhase.BACKEND_INIT,
    "database": StartupPhase.DATABASE,
    "docker": StartupPhase.DOCKER,
    "models": StartupPhase.MODELS,
    "voice": StartupPhase.VOICE,
    "vision": StartupPhase.VISION,
    "frontend": StartupPhase.FRONTEND,
    "websocket": StartupPhase.WEBSOCKET,
    "complete": StartupPhase.COMPLETE,
    "failed": StartupPhase.FAILED,
    "error": StartupPhase.FAILED,
    # v5.0: Hot Reload phases
    "hot_reload_detected": StartupPhase.HOT_RELOAD_DETECTED,
    "hot_reload_restarting": StartupPhase.HOT_RELOAD_RESTARTING,
    "hot_reload_rebuilding": StartupPhase.HOT_RELOAD_REBUILDING,
    "hot_reload_complete": StartupPhase.HOT_RELOAD_COMPLETE,
    # v6.0: Data Flywheel and Learning phases
    "flywheel": StartupPhase.FLYWHEEL_INIT,
    "flywheel_init": StartupPhase.FLYWHEEL_INIT,
    "flywheel_collecting": StartupPhase.FLYWHEEL_COLLECTING,
    "flywheel_training": StartupPhase.FLYWHEEL_TRAINING,
    "flywheel_complete": StartupPhase.FLYWHEEL_COMPLETE,
    "learning": StartupPhase.LEARNING_GOALS,
    "learning_goals": StartupPhase.LEARNING_GOALS,
    "jarvis_prime": StartupPhase.JARVIS_PRIME,
    "prime": StartupPhase.JARVIS_PRIME,
    "reactor_core": StartupPhase.REACTOR_CORE,
    "reactor": StartupPhase.REACTOR_CORE,
    "training": StartupPhase.FLYWHEEL_TRAINING,
    # v93.16: Project Trinity phases
    "trinity": StartupPhase.TRINITY_INIT,
    "trinity_init": StartupPhase.TRINITY_INIT,
    "trinity_body": StartupPhase.TRINITY_BODY,
    "trinity_mind": StartupPhase.TRINITY_MIND,
    "trinity_nerves": StartupPhase.TRINITY_NERVES,
    "trinity_heartbeat": StartupPhase.TRINITY_HEARTBEAT,
    "trinity_ipc": StartupPhase.TRINITY_IPC,
    "trinity_sync": StartupPhase.TRINITY_SYNC,
    "trinity_complete": StartupPhase.TRINITY_COMPLETE,
    "trinity_partial": StartupPhase.TRINITY_PARTIAL,
    # v93.16: Advanced subsystem phases
    "agi_orchestrator": StartupPhase.AGI_ORCHESTRATOR,
    "model_serving": StartupPhase.MODEL_SERVING,
    "agent_registry": StartupPhase.AGENT_REGISTRY,
    "state_manager": StartupPhase.STATE_MANAGER,
    "continuous_learning": StartupPhase.CONTINUOUS_LEARNING,
    "event_bus": StartupPhase.EVENT_BUS,
    "knowledge_graph": StartupPhase.KNOWLEDGE_GRAPH,
    "training_pipeline": StartupPhase.TRAINING_PIPELINE,
    "observability": StartupPhase.OBSERVABILITY,
}


def get_phase_from_stage(stage: str) -> StartupPhase:
    """Convert a progress reporter stage to a StartupPhase."""
    return STAGE_TO_PHASE.get(stage.lower(), StartupPhase.BACKEND_INIT)


# Singleton instance
_startup_narrator: Optional[IntelligentStartupNarrator] = None


def get_startup_narrator(
    config: Optional[NarrationConfig] = None,
    user_name: str = "Sir"
) -> IntelligentStartupNarrator:
    """Get the singleton startup narrator instance."""
    global _startup_narrator
    if _startup_narrator is None:
        _startup_narrator = IntelligentStartupNarrator(config, user_name)
    return _startup_narrator


async def narrate_phase(
    phase: StartupPhase,
    message: str,
    progress: float,
    context: str = "start",
) -> None:
    """Convenience function to narrate a phase."""
    narrator = get_startup_narrator()
    await narrator.announce_phase(phase, message, progress, context)


async def narrate_progress(progress: float, message: Optional[str] = None) -> None:
    """Convenience function to narrate progress."""
    narrator = get_startup_narrator()
    await narrator.announce_progress(progress, message)


async def narrate_complete(message: Optional[str] = None) -> None:
    """Convenience function to narrate completion."""
    narrator = get_startup_narrator()
    await narrator.announce_complete(message)


async def narrate_error(error_message: str) -> None:
    """Convenience function to narrate an error."""
    narrator = get_startup_narrator()
    await narrator.announce_error(error_message)


async def narrate_warning(message: str, context: str = "slow") -> None:
    """v5.0: Convenience function to narrate a warning."""
    narrator = get_startup_narrator()
    await narrator.announce_warning(message, context)


async def narrate_partial_complete(
    services_ready: Optional[List[str]] = None,
    services_failed: Optional[List[str]] = None,
    progress: int = 50,
    duration_seconds: Optional[float] = None,
) -> None:
    """v5.0: Convenience function to narrate partial completion."""
    narrator = get_startup_narrator()
    await narrator.announce_partial_complete(
        services_ready=services_ready,
        services_failed=services_failed,
        progress=progress,
        duration_seconds=duration_seconds,
    )

