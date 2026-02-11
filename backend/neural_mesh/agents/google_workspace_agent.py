"""
JARVIS Neural Mesh - Google Workspace Agent
=============================================

A production agent specialized in Google Workspace administration and communication.
Handles Gmail, Calendar, Drive, Sheets, and Contacts integrations for the "Chief of Staff" role.

**UNIFIED EXECUTION ARCHITECTURE**

This agent implements a "Never-Fail" waterfall strategy:

    Tier 1: Google API (Fast, Cloud-based)
    │       Gmail API, Calendar API, People API, Sheets API
    │       ↓ (if unavailable or fails)
    │
    Tier 2: macOS Local (Native apps via CalendarBridge/AppleScript)
    │       macOS Calendar, macOS Contacts
    │       ↓ (if unavailable or fails)
    │
    Tier 3: Computer Use (Visual automation)
            Screenshot → Claude Vision → Click actions
            Works with ANY app visible on screen

**TRINITY LOOP INTEGRATION (v3.0)**

This agent now integrates with the Trinity Loop:
- Visual Context: Resolves "this", "him/her" from screen OCR text
- Experience Logging: Forwards all interactions to Reactor Core for training
- Entity Resolution: Uses LLM to resolve ambiguous references

Capabilities:
- fetch_unread_emails: Get unread emails with intelligent filtering
- check_calendar_events: View calendar events for any date
- draft_email_reply: Create draft email responses
- send_email: Send emails directly
- search_email: Search emails with advanced queries
- create_calendar_event: Schedule new events
- get_contacts: Retrieve contact information
- workspace_summary: Get daily briefing summary
- create_document: Create Google Docs with AI content generation
- read_spreadsheet: Read data from Google Sheets
- write_spreadsheet: Write data to Google Sheets

This agent handles all "Admin" and "Communication" tasks, enabling JARVIS to:
- "Check my schedule"
- "Draft an email to Mitra"
- "Reply to this email" (with visual context)
- "What meetings do I have today?"
- "Write an essay on dogs"
- "Read the sales data from my spreadsheet"

Author: JARVIS AI System
Version: 3.0.0 (Trinity Integration + Sheets)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)

from ..base.base_neural_mesh_agent import BaseNeuralMeshAgent
from ..data_models import (
    AgentMessage,
    KnowledgeType,
    MessageType,
    MessagePriority,
)

logger = logging.getLogger(__name__)

# =============================================================================
# Trinity Loop Integration - Experience Forwarder
# =============================================================================

EXPERIENCE_FORWARDER_AVAILABLE = False
try:
    from backend.intelligence.cross_repo_experience_forwarder import (
        get_experience_forwarder,
        CrossRepoExperienceForwarder,
    )
    EXPERIENCE_FORWARDER_AVAILABLE = True
except ImportError:
    try:
        from intelligence.cross_repo_experience_forwarder import (
            get_experience_forwarder,
            CrossRepoExperienceForwarder,
        )
        EXPERIENCE_FORWARDER_AVAILABLE = True
    except ImportError:
        get_experience_forwarder = None
        logger.info("Experience forwarder not available - Reactor Core integration disabled")

# =============================================================================
# Entity Resolution - Unified Model Serving
# =============================================================================

UNIFIED_MODEL_SERVING_AVAILABLE = False
try:
    from backend.intelligence.unified_model_serving import get_model_serving
    UNIFIED_MODEL_SERVING_AVAILABLE = True
except ImportError:
    try:
        from intelligence.unified_model_serving import get_model_serving
        UNIFIED_MODEL_SERVING_AVAILABLE = True
    except ImportError:
        get_model_serving = None
        logger.info("Unified model serving not available - entity resolution may be limited")


# =============================================================================
# v3.1: Per-API Circuit Breaker with Adaptive Recovery
# =============================================================================

@dataclass
class CircuitState:
    """State for a single API circuit breaker."""
    failures: int = 0
    successes_since_half_open: int = 0
    last_failure_time: float = 0.0
    state: str = "closed"  # closed, open, half_open
    consecutive_successes: int = 0


class PerAPICircuitBreaker:
    """
    Per-API circuit breaker with adaptive recovery.

    Each Google API (Gmail, Calendar, Drive, Sheets) has its own circuit breaker,
    allowing failures in one API not to affect others.

    States:
    - closed: Normal operation, requests flow through
    - open: Too many failures, requests fail fast
    - half_open: Testing if API has recovered

    Features:
    - Exponential backoff with jitter
    - Adaptive failure threshold based on recent success rate
    - Automatic recovery detection
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 3,
    ):
        self._circuits: Dict[str, CircuitState] = {}
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._half_open_max_calls = half_open_max_calls
        self._lock = asyncio.Lock()

    def _get_circuit(self, api_name: str) -> CircuitState:
        """Get or create circuit for an API."""
        if api_name not in self._circuits:
            self._circuits[api_name] = CircuitState()
        return self._circuits[api_name]

    async def can_execute(self, api_name: str) -> bool:
        """Check if a request can be executed for this API."""
        async with self._lock:
            circuit = self._get_circuit(api_name)
            current_time = asyncio.get_event_loop().time()

            if circuit.state == "closed":
                return True

            elif circuit.state == "open":
                # Check if recovery timeout has elapsed
                if current_time - circuit.last_failure_time >= self._recovery_timeout:
                    circuit.state = "half_open"
                    circuit.successes_since_half_open = 0
                    logger.info(f"[CircuitBreaker] {api_name}: open → half_open")
                    return True
                return False

            elif circuit.state == "half_open":
                # Allow limited requests to test recovery
                return circuit.successes_since_half_open < self._half_open_max_calls

            return True

    async def record_success(self, api_name: str) -> None:
        """Record a successful API call."""
        async with self._lock:
            circuit = self._get_circuit(api_name)
            circuit.consecutive_successes += 1

            if circuit.state == "half_open":
                circuit.successes_since_half_open += 1
                if circuit.successes_since_half_open >= self._half_open_max_calls:
                    circuit.state = "closed"
                    circuit.failures = 0
                    logger.info(f"[CircuitBreaker] {api_name}: half_open → closed (recovered)")

            elif circuit.state == "closed":
                # Reset failure count on success
                circuit.failures = max(0, circuit.failures - 1)

    async def record_failure(self, api_name: str) -> None:
        """Record a failed API call."""
        async with self._lock:
            circuit = self._get_circuit(api_name)
            circuit.failures += 1
            circuit.consecutive_successes = 0
            circuit.last_failure_time = asyncio.get_event_loop().time()

            if circuit.state == "half_open":
                # Failure during half-open goes back to open
                circuit.state = "open"
                logger.warning(f"[CircuitBreaker] {api_name}: half_open → open (still failing)")

            elif circuit.state == "closed" and circuit.failures >= self._failure_threshold:
                circuit.state = "open"
                logger.warning(f"[CircuitBreaker] {api_name}: closed → open (threshold reached)")

    def get_status(self) -> Dict[str, Dict[str, Any]]:
        """Get status of all circuits."""
        return {
            api_name: {
                "state": circuit.state,
                "failures": circuit.failures,
                "consecutive_successes": circuit.consecutive_successes,
            }
            for api_name, circuit in self._circuits.items()
        }


# =============================================================================
# v3.1: Parallel Tier Execution with Race Pattern
# =============================================================================

@dataclass
class TierResult:
    """Result from a tier execution attempt."""
    tier: str
    success: bool
    data: Any = None
    error: Optional[str] = None
    execution_time_ms: float = 0.0


class ParallelTierExecutor:
    """
    Execute operations across multiple tiers in parallel, racing for fastest success.

    Instead of sequential waterfall (try Tier 1, then Tier 2, then Tier 3),
    this executor runs all tiers in parallel and picks the fastest successful result.

    Features:
    - Concurrent execution with asyncio.create_task
    - First-success-wins with automatic cancellation of slower tasks
    - Timeout-based selection (pick first to complete under threshold)
    - Cost-aware selection (prefer cheaper tiers when speed is similar)
    """

    def __init__(
        self,
        default_timeout: float = 10.0,
        prefer_local: bool = True,
    ):
        self._default_timeout = default_timeout
        self._prefer_local = prefer_local
        self._execution_stats: Dict[str, List[float]] = {}

    async def execute_parallel(
        self,
        operations: Dict[str, Callable[[], Awaitable[Any]]],
        timeout: Optional[float] = None,
    ) -> TierResult:
        """
        Execute multiple tier operations in parallel.

        Args:
            operations: Dict of tier_name → async callable
            timeout: Overall timeout in seconds

        Returns:
            TierResult from the fastest successful tier
        """
        timeout = timeout or self._default_timeout

        # Create tasks for all tiers
        tasks: Dict[str, asyncio.Task] = {}
        for tier_name, operation in operations.items():
            task = asyncio.create_task(
                self._execute_tier(tier_name, operation),
                name=f"tier_{tier_name}",
            )
            tasks[tier_name] = task

        # Race for first success
        done_results: List[TierResult] = []
        pending = set(tasks.values())

        # v211.0: Use asyncio.wait_for for Python 3.9 compatibility
        async def _race_for_first_success():
            nonlocal pending, done_results
            while pending:
                done, pending = await asyncio.wait(
                    pending,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                for task in done:
                    try:
                        result = task.result()
                        done_results.append(result)

                        if result.success:
                            # First success - cancel remaining tasks
                            for remaining_task in pending:
                                remaining_task.cancel()

                            # Wait for cancellation to complete
                            if pending:
                                await asyncio.gather(*pending, return_exceptions=True)

                            return result

                    except Exception as e:
                        logger.warning(f"Tier task failed: {e}")
            return None

        try:
            result = await asyncio.wait_for(_race_for_first_success(), timeout=timeout)
            if result is not None:
                return result

        except asyncio.TimeoutError:
            # Cancel all remaining tasks
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

            logger.warning(f"Parallel tier execution timed out after {timeout}s")

        # All tiers failed - return best partial result or error
        if done_results:
            # Return the one with least severe error
            return min(done_results, key=lambda r: 0 if r.success else 1)

        return TierResult(
            tier="none",
            success=False,
            error=f"All tiers failed or timed out after {timeout}s",
        )

    async def _execute_tier(
        self,
        tier_name: str,
        operation: Callable[[], Awaitable[Any]],
    ) -> TierResult:
        """Execute a single tier operation with timing."""
        import time as time_module
        start_time = time_module.time()

        try:
            result = await operation()
            execution_time_ms = (time_module.time() - start_time) * 1000

            # Track execution time for this tier
            if tier_name not in self._execution_stats:
                self._execution_stats[tier_name] = []
            self._execution_stats[tier_name].append(execution_time_ms)
            if len(self._execution_stats[tier_name]) > 100:
                self._execution_stats[tier_name] = self._execution_stats[tier_name][-100:]

            return TierResult(
                tier=tier_name,
                success=True,
                data=result,
                execution_time_ms=execution_time_ms,
            )

        except Exception as e:
            execution_time_ms = (time_module.time() - start_time) * 1000
            return TierResult(
                tier=tier_name,
                success=False,
                error=str(e),
                execution_time_ms=execution_time_ms,
            )

    def get_tier_stats(self) -> Dict[str, Dict[str, float]]:
        """Get average execution times per tier."""
        return {
            tier: {
                "avg_time_ms": sum(times) / len(times) if times else 0,
                "min_time_ms": min(times) if times else 0,
                "max_time_ms": max(times) if times else 0,
                "sample_count": len(times),
            }
            for tier, times in self._execution_stats.items()
        }


# Global instances for per-API circuit breaker and parallel executor
_api_circuit_breaker: Optional[PerAPICircuitBreaker] = None
_parallel_executor: Optional[ParallelTierExecutor] = None


def get_api_circuit_breaker() -> PerAPICircuitBreaker:
    """Get the global per-API circuit breaker instance."""
    global _api_circuit_breaker
    if _api_circuit_breaker is None:
        _api_circuit_breaker = PerAPICircuitBreaker()
    return _api_circuit_breaker


def get_parallel_executor() -> ParallelTierExecutor:
    """Get the global parallel tier executor instance."""
    global _parallel_executor
    if _parallel_executor is None:
        _parallel_executor = ParallelTierExecutor()
    return _parallel_executor


# =============================================================================
# Google API Availability Check
# =============================================================================

try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    import base64
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    GOOGLE_API_AVAILABLE = True
except ImportError:
    GOOGLE_API_AVAILABLE = False
    logger.warning(
        "Google API libraries not available. Install: "
        "pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client"
    )


# =============================================================================
# Tier 2: macOS Local Availability Check (CalendarBridge)
# =============================================================================

try:
    from backend.system_control.calendar_bridge import CalendarBridge, CalendarEvent
    CALENDAR_BRIDGE_AVAILABLE = True
except ImportError:
    try:
        from system_control.calendar_bridge import CalendarBridge, CalendarEvent
        CALENDAR_BRIDGE_AVAILABLE = True
    except ImportError:
        CALENDAR_BRIDGE_AVAILABLE = False
        CalendarBridge = None
        CalendarEvent = None
        logger.info("CalendarBridge not available - macOS local calendar fallback disabled")


# =============================================================================
# Tier 3: Computer Use Availability Check (Visual Fallback)
# =============================================================================

try:
    from backend.autonomy.computer_use_tool import (
        ComputerUseTool,
        get_computer_use_tool,
        ComputerUseResult,
    )
    COMPUTER_USE_AVAILABLE = True
except ImportError:
    try:
        from autonomy.computer_use_tool import (
            ComputerUseTool,
            get_computer_use_tool,
            ComputerUseResult,
        )
        COMPUTER_USE_AVAILABLE = True
    except ImportError:
        COMPUTER_USE_AVAILABLE = False
        ComputerUseTool = None
        get_computer_use_tool = None
        logger.info("ComputerUseTool not available - visual fallback disabled")


# =============================================================================
# Document Writer (Google Docs + AI Content)
# =============================================================================

try:
    from backend.context_intelligence.executors.document_writer import (
        DocumentWriterExecutor,
        DocumentRequest,
        DocumentType,
        DocumentFormat,
        get_document_writer,
    )
    DOCUMENT_WRITER_AVAILABLE = True
except ImportError:
    try:
        from context_intelligence.executors.document_writer import (
            DocumentWriterExecutor,
            DocumentRequest,
            DocumentType,
            DocumentFormat,
            get_document_writer,
        )
        DOCUMENT_WRITER_AVAILABLE = True
    except ImportError:
        DOCUMENT_WRITER_AVAILABLE = False
        DocumentWriterExecutor = None
        get_document_writer = None
        logger.info("DocumentWriterExecutor not available - document creation disabled")


# =============================================================================
# Google Docs API (Direct)
# =============================================================================

try:
    from backend.context_intelligence.automation.google_docs_api import (
        GoogleDocsClient,
        get_google_docs_client,
    )
    GOOGLE_DOCS_AVAILABLE = True
except ImportError:
    try:
        from context_intelligence.automation.google_docs_api import (
            GoogleDocsClient,
            get_google_docs_client,
        )
        GOOGLE_DOCS_AVAILABLE = True
    except ImportError:
        GOOGLE_DOCS_AVAILABLE = False
        GoogleDocsClient = None
        get_google_docs_client = None
        logger.info("GoogleDocsClient not available - Google Docs creation disabled")


# =============================================================================
# Google Sheets API Availability Check
# =============================================================================

GOOGLE_SHEETS_AVAILABLE = False
try:
    import gspread
    from google.oauth2.service_account import Credentials as ServiceAccountCredentials
    GOOGLE_SHEETS_AVAILABLE = True
except ImportError:
    gspread = None
    logger.info("gspread not available - install with: pip install gspread")


# =============================================================================
# Configuration
# =============================================================================

# OAuth 2.0 scopes for Google Workspace
GOOGLE_WORKSPACE_SCOPES = [
    # Gmail
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.send',
    'https://www.googleapis.com/auth/gmail.compose',
    'https://www.googleapis.com/auth/gmail.modify',
    # Calendar
    'https://www.googleapis.com/auth/calendar.readonly',
    'https://www.googleapis.com/auth/calendar.events',
    # Drive (for attachments)
    'https://www.googleapis.com/auth/drive.file',
    # Contacts
    'https://www.googleapis.com/auth/contacts.readonly',
]


@dataclass
class GoogleWorkspaceConfig:
    """
    Configuration for Google Workspace Agent.

    Inherits all base agent configuration from BaseAgentConfig via composition.
    This ensures compatibility with Neural Mesh infrastructure while maintaining
    agent-specific Google Workspace settings.
    """
    # Base agent configuration (inherited attributes)
    # These are required by BaseNeuralMeshAgent
    heartbeat_interval_seconds: float = 10.0  # Heartbeat frequency
    message_queue_size: int = 1000  # Message queue capacity
    message_handler_timeout_seconds: float = 10.0  # Message processing timeout
    enable_knowledge_access: bool = True  # Enable knowledge graph access
    knowledge_cache_size: int = 100  # Local knowledge cache size
    log_messages: bool = True  # Log message traffic
    log_level: str = "INFO"  # Logging level

    # Google Workspace specific configuration
    credentials_path: str = field(
        default_factory=lambda: os.getenv(
            'GOOGLE_CREDENTIALS_PATH',
            str(Path.home() / '.jarvis' / 'google_credentials.json')
        )
    )
    token_path: str = field(
        default_factory=lambda: os.getenv(
            'GOOGLE_TOKEN_PATH',
            str(Path.home() / '.jarvis' / 'google_workspace_token.json')
        )
    )
    # Email defaults
    default_email_limit: int = 10
    max_email_body_preview: int = 500
    # Calendar defaults
    calendar_lookahead_days: int = 7
    default_event_duration_minutes: int = 60
    # Caching
    cache_ttl_seconds: float = 300.0  # 5 minutes
    # Retry
    max_retries: int = 3
    retry_delay_seconds: float = 1.0


# =============================================================================
# Intent Detection for Routing
# =============================================================================

class WorkspaceIntent(Enum):
    """Types of workspace intents this agent handles."""

    # Email
    CHECK_EMAIL = "check_email"
    SEND_EMAIL = "send_email"
    DRAFT_EMAIL = "draft_email"
    SEARCH_EMAIL = "search_email"

    # Calendar
    CHECK_CALENDAR = "check_calendar"
    CREATE_EVENT = "create_event"
    FIND_FREE_TIME = "find_free_time"

    # General
    DAILY_BRIEFING = "daily_briefing"
    GET_CONTACTS = "get_contacts"

    # Unknown
    UNKNOWN = "unknown"

    # Document creation
    CREATE_DOCUMENT = "create_document"


class ExecutionTier(Enum):
    """Execution tier for the waterfall fallback strategy."""

    GOOGLE_API = "google_api"       # Tier 1: Google Cloud APIs
    MACOS_LOCAL = "macos_local"     # Tier 2: macOS native apps
    COMPUTER_USE = "computer_use"   # Tier 3: Visual automation


@dataclass
class ExecutionResult:
    """Result of a tiered execution attempt."""

    success: bool
    tier_used: ExecutionTier
    data: Dict[str, Any]
    error: Optional[str] = None
    fallback_attempted: bool = False
    execution_time_ms: float = 0.0


class UnifiedWorkspaceExecutor:
    """
    Unified executor implementing the "Never-Fail" waterfall strategy.

    This executor tries each tier in order until one succeeds:
    1. Google API (fast, cloud-based)
    2. macOS Local (CalendarBridge, AppleScript)
    2.5. Spatial Awareness (v6.2: Switch to app via Yabai before Computer Use)
    3. Computer Use (visual automation via Claude Vision)

    Features:
    - Graceful degradation (no crashes on missing components)
    - Automatic tier detection based on availability
    - Parallel execution where possible
    - Detailed logging for debugging
    - Learning from failures for future optimization
    - v6.2 Grand Unification: Spatial Awareness integration
    """

    def __init__(self) -> None:
        """Initialize the unified executor with all available tiers."""
        self._available_tiers: List[ExecutionTier] = []
        self._tier_stats: Dict[ExecutionTier, Dict[str, int]] = {}
        self._calendar_bridge: Optional[CalendarBridge] = None
        self._computer_use: Optional[ComputerUseTool] = None
        self._spatial_awareness = None  # v6.2: SpatialAwarenessAgent integration
        self._initialized = False
        self._lock = asyncio.Lock()

        # Track availability
        self._check_tier_availability()

    def _check_tier_availability(self) -> None:
        """Check which execution tiers are available."""
        self._available_tiers = []

        # Tier 1: Google API
        if GOOGLE_API_AVAILABLE:
            self._available_tiers.append(ExecutionTier.GOOGLE_API)
            logger.info("Tier 1 (Google API) available")

        # Tier 2: macOS Local
        if CALENDAR_BRIDGE_AVAILABLE:
            self._available_tiers.append(ExecutionTier.MACOS_LOCAL)
            logger.info("Tier 2 (macOS Local) available")

        # Tier 3: Computer Use
        if COMPUTER_USE_AVAILABLE:
            self._available_tiers.append(ExecutionTier.COMPUTER_USE)
            logger.info("Tier 3 (Computer Use) available")

        # Initialize stats
        for tier in ExecutionTier:
            self._tier_stats[tier] = {
                "attempts": 0,
                "successes": 0,
                "failures": 0,
            }

        if not self._available_tiers:
            logger.warning(
                "No execution tiers available! "
                "Install Google API libraries, or ensure macOS Calendar access, "
                "or enable Computer Use."
            )

    async def initialize(self) -> bool:
        """Initialize all available execution backends."""
        async with self._lock:
            if self._initialized:
                return True

            try:
                # Initialize Tier 2: CalendarBridge
                if CALENDAR_BRIDGE_AVAILABLE and CalendarBridge is not None:
                    self._calendar_bridge = CalendarBridge()
                    logger.info("CalendarBridge initialized")

                # v6.2: Initialize Spatial Awareness (for smart app switching)
                try:
                    from core.computer_use_bridge import switch_to_app_smart, get_current_context
                    self._spatial_awareness = {
                        "switch_to_app": switch_to_app_smart,
                        "get_context": get_current_context,
                    }
                    logger.info("Spatial Awareness (Proprioception) initialized")
                except ImportError as e:
                    logger.info(f"Spatial Awareness not available: {e}")
                    self._spatial_awareness = None

                # Initialize Tier 3: Computer Use
                if COMPUTER_USE_AVAILABLE and get_computer_use_tool is not None:
                    self._computer_use = get_computer_use_tool()
                    logger.info("ComputerUseTool initialized")

                self._initialized = True
                return True

            except Exception as e:
                logger.exception(f"Error initializing unified executor: {e}")
                return False

    async def execute_calendar_check(
        self,
        google_client: Optional[Any],
        date_str: str = "today",
        hours_ahead: int = 24,
    ) -> ExecutionResult:
        """
        Check calendar using waterfall strategy.

        Tries:
        1. Google Calendar API
        2. macOS CalendarBridge
        3. Computer Use (open Calendar.app, screenshot, analyze)
        """
        start_time = asyncio.get_event_loop().time()

        # Tier 1: Google Calendar API
        if ExecutionTier.GOOGLE_API in self._available_tiers and google_client:
            self._tier_stats[ExecutionTier.GOOGLE_API]["attempts"] += 1
            try:
                result = await google_client.get_calendar_events(date_str=date_str)
                if result and "error" not in result:
                    self._tier_stats[ExecutionTier.GOOGLE_API]["successes"] += 1
                    return ExecutionResult(
                        success=True,
                        tier_used=ExecutionTier.GOOGLE_API,
                        data=result,
                        execution_time_ms=(asyncio.get_event_loop().time() - start_time) * 1000,
                    )
                logger.info("Google Calendar API failed, trying next tier...")
                self._tier_stats[ExecutionTier.GOOGLE_API]["failures"] += 1
            except Exception as e:
                logger.warning(f"Google Calendar API error: {e}")
                self._tier_stats[ExecutionTier.GOOGLE_API]["failures"] += 1

        # Tier 2: macOS CalendarBridge
        if ExecutionTier.MACOS_LOCAL in self._available_tiers and self._calendar_bridge:
            self._tier_stats[ExecutionTier.MACOS_LOCAL]["attempts"] += 1
            try:
                events = await self._calendar_bridge.get_events(hours_ahead=hours_ahead)
                if events is not None:
                    # Convert CalendarEvent objects to dicts
                    event_dicts = []
                    for event in events:
                        event_dicts.append({
                            "id": event.event_id,
                            "title": event.title,
                            "start": event.start_time.isoformat(),
                            "end": event.end_time.isoformat(),
                            "location": event.location,
                            "is_all_day": event.is_all_day,
                            "source": "macos_calendar",
                        })
                    self._tier_stats[ExecutionTier.MACOS_LOCAL]["successes"] += 1
                    return ExecutionResult(
                        success=True,
                        tier_used=ExecutionTier.MACOS_LOCAL,
                        data={"events": event_dicts, "count": len(event_dicts)},
                        fallback_attempted=True,
                        execution_time_ms=(asyncio.get_event_loop().time() - start_time) * 1000,
                    )
                logger.info("macOS Calendar failed, trying Computer Use...")
                self._tier_stats[ExecutionTier.MACOS_LOCAL]["failures"] += 1
            except Exception as e:
                logger.warning(f"macOS Calendar error: {e}")
                self._tier_stats[ExecutionTier.MACOS_LOCAL]["failures"] += 1

        # Tier 3: Computer Use (Visual)
        # v6.2: First switch to Calendar using Spatial Awareness, then take screenshot
        if ExecutionTier.COMPUTER_USE in self._available_tiers and self._computer_use:
            self._tier_stats[ExecutionTier.COMPUTER_USE]["attempts"] += 1
            try:
                # v6.2 Grand Unification: Switch to Calendar app first via Yabai
                await self._switch_to_app_with_spatial_awareness("Calendar", narrate=True)

                # Now run Computer Use - Calendar should already be focused
                goal = f"Read the calendar events currently visible on screen. List all meetings and appointments for {date_str}."
                result = await self._computer_use.run(goal=goal)
                if result and result.success:
                    self._tier_stats[ExecutionTier.COMPUTER_USE]["successes"] += 1
                    return ExecutionResult(
                        success=True,
                        tier_used=ExecutionTier.COMPUTER_USE,
                        data={
                            "raw_response": result.final_message,
                            "actions_count": result.actions_count,
                            "source": "computer_use_visual",
                        },
                        fallback_attempted=True,
                        execution_time_ms=(asyncio.get_event_loop().time() - start_time) * 1000,
                    )
                self._tier_stats[ExecutionTier.COMPUTER_USE]["failures"] += 1
            except Exception as e:
                logger.warning(f"Computer Use error: {e}")
                self._tier_stats[ExecutionTier.COMPUTER_USE]["failures"] += 1

        # All tiers failed
        return ExecutionResult(
            success=False,
            tier_used=ExecutionTier.GOOGLE_API,
            data={},
            error="All execution tiers failed for calendar check",
            execution_time_ms=(asyncio.get_event_loop().time() - start_time) * 1000,
        )

    async def execute_email_check(
        self,
        google_client: Optional[Any],
        limit: int = 10,
    ) -> ExecutionResult:
        """
        Check emails using waterfall strategy.

        Tries:
        1. Gmail API
        2. Computer Use (open Mail.app or Gmail in browser)
        """
        start_time = asyncio.get_event_loop().time()

        # Tier 1: Gmail API
        if ExecutionTier.GOOGLE_API in self._available_tiers and google_client:
            self._tier_stats[ExecutionTier.GOOGLE_API]["attempts"] += 1
            try:
                result = await google_client.fetch_unread_emails(limit=limit)
                if result and "error" not in result:
                    self._tier_stats[ExecutionTier.GOOGLE_API]["successes"] += 1
                    return ExecutionResult(
                        success=True,
                        tier_used=ExecutionTier.GOOGLE_API,
                        data=result,
                        execution_time_ms=(asyncio.get_event_loop().time() - start_time) * 1000,
                    )
                self._tier_stats[ExecutionTier.GOOGLE_API]["failures"] += 1
            except Exception as e:
                logger.warning(f"Gmail API error: {e}")
                self._tier_stats[ExecutionTier.GOOGLE_API]["failures"] += 1

        # Tier 3: Computer Use (Visual) - Skip Tier 2 for email (no macOS email bridge)
        # v6.2: First switch to browser using Spatial Awareness, then navigate
        if ExecutionTier.COMPUTER_USE in self._available_tiers and self._computer_use:
            self._tier_stats[ExecutionTier.COMPUTER_USE]["attempts"] += 1
            try:
                # v6.2 Grand Unification: Switch to Safari first via Yabai
                await self._switch_to_app_with_spatial_awareness("Safari", narrate=True)

                # Now run Computer Use - Safari should already be focused
                goal = f"Navigate to mail.google.com and read the {limit} most recent unread emails. List the sender and subject of each."
                result = await self._computer_use.run(goal=goal)
                if result and result.success:
                    self._tier_stats[ExecutionTier.COMPUTER_USE]["successes"] += 1
                    return ExecutionResult(
                        success=True,
                        tier_used=ExecutionTier.COMPUTER_USE,
                        data={
                            "raw_response": result.final_message,
                            "actions_count": result.actions_count,
                            "source": "computer_use_visual",
                        },
                        fallback_attempted=True,
                        execution_time_ms=(asyncio.get_event_loop().time() - start_time) * 1000,
                    )
                self._tier_stats[ExecutionTier.COMPUTER_USE]["failures"] += 1
            except Exception as e:
                logger.warning(f"Computer Use error for email: {e}")
                self._tier_stats[ExecutionTier.COMPUTER_USE]["failures"] += 1

        return ExecutionResult(
            success=False,
            tier_used=ExecutionTier.GOOGLE_API,
            data={},
            error="All execution tiers failed for email check",
            execution_time_ms=(asyncio.get_event_loop().time() - start_time) * 1000,
        )

    async def execute_document_creation(
        self,
        topic: str,
        document_type: str = "essay",
        word_count: Optional[int] = None,
    ) -> ExecutionResult:
        """
        Create a document using waterfall strategy.

        Tries:
        1. Google Docs API + Claude for content
        2. Computer Use (open Google Docs in browser, type content)
        """
        start_time = asyncio.get_event_loop().time()

        # Tier 1: Google Docs API via DocumentWriter
        if DOCUMENT_WRITER_AVAILABLE and get_document_writer is not None:
            try:
                writer = get_document_writer()

                # Convert string to DocumentType enum
                doc_type = DocumentType.ESSAY
                if document_type.lower() == "report":
                    doc_type = DocumentType.REPORT
                elif document_type.lower() == "paper":
                    doc_type = DocumentType.PAPER

                request = DocumentRequest(
                    topic=topic,
                    document_type=doc_type,
                    word_count=word_count,
                )

                result = await writer.create_document(request)
                if result.get("success"):
                    return ExecutionResult(
                        success=True,
                        tier_used=ExecutionTier.GOOGLE_API,
                        data=result,
                        execution_time_ms=(asyncio.get_event_loop().time() - start_time) * 1000,
                    )
            except Exception as e:
                logger.warning(f"DocumentWriter error: {e}")

        # Tier 3: Computer Use (Visual)
        # v6.2: First switch to browser using Spatial Awareness
        if ExecutionTier.COMPUTER_USE in self._available_tiers and self._computer_use:
            try:
                # v6.2 Grand Unification: Switch to Safari first via Yabai
                await self._switch_to_app_with_spatial_awareness("Safari", narrate=True)

                goal = (
                    f"Navigate to docs.google.com, create a new blank document, "
                    f"title it '{topic}', and write a {word_count or 500} word {document_type} about {topic}."
                )
                result = await self._computer_use.run(goal=goal)
                if result and result.success:
                    return ExecutionResult(
                        success=True,
                        tier_used=ExecutionTier.COMPUTER_USE,
                        data={
                            "raw_response": result.final_message,
                            "source": "computer_use_visual",
                        },
                        fallback_attempted=True,
                        execution_time_ms=(asyncio.get_event_loop().time() - start_time) * 1000,
                    )
            except Exception as e:
                logger.warning(f"Computer Use error for document: {e}")

        return ExecutionResult(
            success=False,
            tier_used=ExecutionTier.GOOGLE_API,
            data={},
            error="All execution tiers failed for document creation",
            execution_time_ms=(asyncio.get_event_loop().time() - start_time) * 1000,
        )

    async def _switch_to_app_with_spatial_awareness(
        self,
        app_name: str,
        narrate: bool = True,
    ) -> bool:
        """
        v6.2 Grand Unification: Switch to an app using Spatial Awareness.

        Before Computer Use takes a screenshot, we use Yabai to teleport
        to the correct app/window across all macOS Spaces.

        Args:
            app_name: Name of the app to switch to (e.g., "Calendar", "Safari")
            narrate: Whether to speak the switch action

        Returns:
            True if switch succeeded, False otherwise
        """
        if not self._spatial_awareness:
            logger.debug("Spatial Awareness not available, skipping app switch")
            return False

        try:
            switch_fn = self._spatial_awareness.get("switch_to_app")
            if not switch_fn:
                return False

            logger.info(f"[Spatial Awareness] Switching to {app_name}...")
            result = await switch_fn(app_name, narrate=narrate)

            # Check if switch was successful
            from core.computer_use_bridge import SwitchResult
            is_success = result.result in (
                SwitchResult.SUCCESS,
                SwitchResult.ALREADY_FOCUSED,
                SwitchResult.SWITCHED_SPACE,
                SwitchResult.LAUNCHED_APP,
            )

            if is_success:
                logger.info(
                    f"[Spatial Awareness] Successfully switched to {app_name} "
                    f"(Space {result.from_space} -> {result.to_space})"
                )
            else:
                logger.warning(
                    f"[Spatial Awareness] Failed to switch to {app_name}: {result.result.value}"
                )

            return is_success

        except Exception as e:
            logger.warning(f"[Spatial Awareness] Error switching to {app_name}: {e}")
            return False

    def get_stats(self) -> Dict[str, Any]:
        """Get execution statistics for all tiers."""
        return {
            "available_tiers": [t.value for t in self._available_tiers],
            "tier_stats": {t.value: stats for t, stats in self._tier_stats.items()},
            "initialized": self._initialized,
            "spatial_awareness_available": self._spatial_awareness is not None,
        }


class WorkspaceIntentDetector:
    """
    Detects workspace-related intents from natural language queries.

    This enables intelligent routing so that queries like:
    - "Check my schedule" → CHECK_CALENDAR
    - "Draft an email to Mitra" → DRAFT_EMAIL
    - "What meetings today?" → CHECK_CALENDAR
    """

    # Intent patterns (lowercase) - more precise patterns that must match as phrases
    INTENT_PATTERNS: Dict[WorkspaceIntent, List[str]] = {
        WorkspaceIntent.DRAFT_EMAIL: [
            "draft email", "draft an email", "write email", "compose email",
            "draft reply", "write a reply", "draft response", "draft to",
            "write an email", "compose a reply",
        ],
        WorkspaceIntent.SEND_EMAIL: [
            "send email", "send an email", "send message", "send a message",
            "email to", "message to",
        ],
        WorkspaceIntent.CHECK_EMAIL: [
            "check email", "check my email", "any emails", "new emails",
            "any new emails", "unread email", "unread emails", "my inbox",
            "show inbox", "what emails", "read my email", "show email",
            "show my email", "check inbox", "any new mail", "check mail",
        ],
        WorkspaceIntent.SEARCH_EMAIL: [
            "search email", "find email", "look for email", "emails from",
            "emails about", "emails containing", "search inbox", "find emails",
        ],
        WorkspaceIntent.CHECK_CALENDAR: [
            "check calendar", "check my calendar", "my schedule", "my meetings",
            "what's on my calendar", "calendar today", "upcoming events",
            "what meetings", "events today", "what's on today",
            "agenda", "appointments", "busy today", "today's calendar",
            "schedule today", "schedule for today", "meetings today",
            "what do i have today", "what's happening today",
        ],
        WorkspaceIntent.CREATE_EVENT: [
            "schedule meeting", "create event", "add event", "schedule event",
            "book meeting", "set up meeting", "calendar event", "add to calendar",
            "create a meeting", "schedule a meeting",
        ],
        WorkspaceIntent.FIND_FREE_TIME: [
            "when am i free", "free time", "my availability", "open slots",
            "find time", "when available", "schedule time", "free slots",
        ],
        WorkspaceIntent.DAILY_BRIEFING: [
            "daily briefing", "morning briefing", "daily summary",
            "today's agenda", "brief me", "catch me up", "what's today",
            "give me a briefing", "morning summary", "give me my briefing",
        ],
        WorkspaceIntent.GET_CONTACTS: [
            "contact info", "email address for", "phone number for",
            "contact for", "find contact", "get contact",
        ],
        WorkspaceIntent.CREATE_DOCUMENT: [
            "write an essay", "write essay", "create document", "create a document",
            "write a paper", "write paper", "write a report", "write report",
            "create google doc", "make a document", "write about",
            "essay on", "essay about", "paper on", "paper about",
            "report on", "report about", "article on", "article about",
        ],
    }

    # Required keywords for each intent (at least one must be present for match)
    REQUIRED_KEYWORDS: Dict[WorkspaceIntent, Set[str]] = {
        WorkspaceIntent.CHECK_EMAIL: {"email", "emails", "inbox", "mail"},
        WorkspaceIntent.SEND_EMAIL: {"send", "email"},
        WorkspaceIntent.DRAFT_EMAIL: {"draft", "compose", "write", "email"},
        WorkspaceIntent.SEARCH_EMAIL: {"search", "find", "email", "emails"},
        WorkspaceIntent.CHECK_CALENDAR: {"calendar", "schedule", "meeting", "meetings", "agenda", "events", "appointments"},
        WorkspaceIntent.CREATE_EVENT: {"schedule", "create", "add", "book", "meeting", "event"},
        WorkspaceIntent.FIND_FREE_TIME: {"free", "available", "availability"},
        WorkspaceIntent.DAILY_BRIEFING: {"briefing", "summary", "brief", "catch"},  # "catch me up"
        WorkspaceIntent.GET_CONTACTS: {"contact", "phone", "address"},
        WorkspaceIntent.CREATE_DOCUMENT: {"essay", "paper", "report", "document", "article", "write"},
    }

    # Name extraction patterns
    NAME_PATTERNS = [
        r"email (?:to|for) (\w+)",
        r"message (?:to|for) (\w+)",
        r"draft (?:to|for) (\w+)",
        r"contact (?:info )?(?:for )?(\w+)",
        r"meeting with (\w+)",
        r"schedule with (\w+)",
        r"to (\w+)$",  # "send email to John"
    ]

    def detect(self, query: str) -> Tuple[WorkspaceIntent, float, Dict[str, Any]]:
        """
        Detect workspace intent from a natural language query.

        Args:
            query: The user's query

        Returns:
            Tuple of (intent, confidence, metadata)
        """
        query_lower = query.lower().strip()
        # Strip punctuation from words for keyword matching
        query_words = set(
            word.strip("?!.,;:'\"") for word in query_lower.split()
        )

        # Score each intent
        scores: Dict[WorkspaceIntent, float] = {}

        for intent, patterns in self.INTENT_PATTERNS.items():
            # First check if required keywords are present
            required = self.REQUIRED_KEYWORDS.get(intent, set())
            if required and not any(kw in query_words for kw in required):
                continue  # Skip this intent if no required keywords

            score = 0.0
            matched_patterns = []

            for pattern in patterns:
                if pattern in query_lower:
                    # Full phrase match gets high score
                    score += 2.0
                    matched_patterns.append(pattern)

            # Only count if we had phrase matches
            if score > 0:
                scores[intent] = score

        if not scores:
            return WorkspaceIntent.UNKNOWN, 0.0, {}

        # Get best match
        best_intent = max(scores, key=scores.get)
        best_score = scores[best_intent]

        # Normalize confidence (2.0 per pattern match, expect 1-2 matches for good confidence)
        confidence = min(1.0, best_score / 4.0)

        # Extract metadata
        metadata = {
            "matched_intent": best_intent.value,
            "all_scores": {k.value: v for k, v in scores.items()},
            "extracted_names": self._extract_names(query),
            "extracted_dates": self._extract_dates(query),
        }

        return best_intent, confidence, metadata

    def _extract_names(self, query: str) -> List[str]:
        """Extract person names from query."""
        names = []
        for pattern in self.NAME_PATTERNS:
            matches = re.findall(pattern, query, re.IGNORECASE)
            names.extend(matches)
        return list(set(names))

    def _extract_dates(self, query: str) -> Dict[str, Any]:
        """Extract date references from query."""
        query_lower = query.lower()
        dates = {}

        if "today" in query_lower:
            dates["today"] = date.today().isoformat()
        if "tomorrow" in query_lower:
            dates["tomorrow"] = (date.today() + timedelta(days=1)).isoformat()
        if "yesterday" in query_lower:
            dates["yesterday"] = (date.today() - timedelta(days=1)).isoformat()
        if "this week" in query_lower:
            dates["week_start"] = (date.today() - timedelta(days=date.today().weekday())).isoformat()
            dates["week_end"] = (date.today() + timedelta(days=6 - date.today().weekday())).isoformat()
        if "next week" in query_lower:
            next_monday = date.today() + timedelta(days=7 - date.today().weekday())
            dates["next_week_start"] = next_monday.isoformat()
            dates["next_week_end"] = (next_monday + timedelta(days=6)).isoformat()

        return dates

    def is_workspace_query(self, query: str) -> Tuple[bool, float]:
        """
        Check if a query is workspace-related (for routing decisions).

        Returns:
            Tuple of (is_workspace_related, confidence)
        """
        intent, confidence, _ = self.detect(query)
        is_workspace = intent != WorkspaceIntent.UNKNOWN
        return is_workspace, confidence


# =============================================================================
# Google API Client
# =============================================================================

class GoogleWorkspaceClient:
    """
    Async-compatible client for Google Workspace APIs.

    Handles authentication and provides methods for:
    - Gmail operations
    - Calendar operations
    - Contacts operations

    v3.1 Enhancements:
    - Proactive OAuth token refresh
    - Token expiration monitoring
    - Automatic retry with fresh token on 401
    """

    def __init__(self, config: Optional[GoogleWorkspaceConfig] = None):
        """Initialize the Google Workspace client."""
        self.config = config or GoogleWorkspaceConfig()
        self._creds: Optional[Any] = None
        self._gmail_service = None
        self._calendar_service = None
        self._people_service = None
        self._authenticated = False
        self._lock = asyncio.Lock()

        # Cache
        self._cache: Dict[str, Tuple[Any, float]] = {}

        # v3.1: Token management
        self._token_refresh_buffer = 300  # Refresh 5 minutes before expiry
        self._last_token_check = 0.0
        self._token_refresh_task: Optional[asyncio.Task] = None

    async def _ensure_valid_token(self) -> bool:
        """
        Proactively check and refresh token before expiration.

        v3.1: Called before each API operation to ensure token is valid.
        Refreshes token if it will expire within the buffer period.

        Returns:
            True if token is valid or was successfully refreshed
        """
        if not self._creds:
            return False

        import time as time_module
        current_time = time_module.time()

        # Check token expiry
        try:
            if hasattr(self._creds, 'expired') and self._creds.expired:
                logger.info("[GoogleWorkspaceClient] Token expired, refreshing...")
                return await self._refresh_token()

            if hasattr(self._creds, 'expiry') and self._creds.expiry:
                # Calculate time until expiry
                from datetime import timezone
                expiry_ts = self._creds.expiry.replace(tzinfo=timezone.utc).timestamp()
                time_until_expiry = expiry_ts - current_time

                if time_until_expiry < self._token_refresh_buffer:
                    logger.info(
                        f"[GoogleWorkspaceClient] Token expires in {time_until_expiry:.0f}s, "
                        f"proactively refreshing..."
                    )
                    return await self._refresh_token()

            return True

        except Exception as e:
            logger.warning(f"Token check failed: {e}")
            return True  # Proceed anyway, let API call fail if token is bad

    async def _refresh_token(self) -> bool:
        """
        Refresh the OAuth token.

        v3.1: Handles token refresh with proper locking to prevent race conditions.
        """
        async with self._lock:
            try:
                if not self._creds or not hasattr(self._creds, 'refresh'):
                    return False

                # Run refresh in thread pool (it's blocking)
                loop = asyncio.get_event_loop()

                def do_refresh():
                    from google.auth.transport.requests import Request
                    self._creds.refresh(Request())
                    return True

                success = await loop.run_in_executor(None, do_refresh)

                if success:
                    logger.info("[GoogleWorkspaceClient] Token refreshed successfully")
                    # Re-build services with new token
                    await self._rebuild_services()

                return success

            except Exception as e:
                logger.error(f"Token refresh failed: {e}")
                return False

    async def _rebuild_services(self) -> None:
        """Rebuild Google API services with fresh credentials."""
        try:
            loop = asyncio.get_event_loop()

            def build_services():
                if GOOGLE_API_AVAILABLE:
                    from googleapiclient.discovery import build
                    self._gmail_service = build('gmail', 'v1', credentials=self._creds)
                    self._calendar_service = build('calendar', 'v3', credentials=self._creds)
                    self._people_service = build('people', 'v1', credentials=self._creds)

            await loop.run_in_executor(None, build_services)

        except Exception as e:
            logger.warning(f"Service rebuild failed: {e}")

    async def _execute_with_retry(
        self,
        operation: Callable[[], Any],
        api_name: str = "google_api",
    ) -> Any:
        """
        Execute an API operation with automatic retry on token expiration.

        v3.1: Catches 401 errors and retries with refreshed token.
        Also integrates with per-API circuit breaker.
        """
        circuit_breaker = get_api_circuit_breaker()

        # Check circuit breaker
        if not await circuit_breaker.can_execute(api_name):
            raise RuntimeError(f"Circuit breaker open for {api_name}")

        # Ensure valid token before operation
        await self._ensure_valid_token()

        try:
            # Run operation in thread pool
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, operation)

            # Record success
            await circuit_breaker.record_success(api_name)

            return result

        except Exception as e:
            error_str = str(e).lower()

            # Check for auth errors
            if "401" in error_str or "unauthorized" in error_str or "invalid_grant" in error_str:
                logger.warning(f"[GoogleWorkspaceClient] Auth error, attempting token refresh...")

                # Refresh token and retry once
                if await self._refresh_token():
                    try:
                        result = await loop.run_in_executor(None, operation)
                        await circuit_breaker.record_success(api_name)
                        return result
                    except Exception as retry_error:
                        await circuit_breaker.record_failure(api_name)
                        raise retry_error

            # Record failure
            await circuit_breaker.record_failure(api_name)
            raise

    async def authenticate(self) -> bool:
        """
        Authenticate with Google APIs.

        Returns:
            True if authentication successful
        """
        if not GOOGLE_API_AVAILABLE:
            logger.error("Google API libraries not available")
            return False

        async with self._lock:
            if self._authenticated:
                return True

            try:
                # Run OAuth in thread pool (it's blocking)
                loop = asyncio.get_event_loop()
                success = await loop.run_in_executor(
                    None, self._authenticate_sync
                )
                self._authenticated = success
                return success

            except Exception as e:
                logger.exception(f"Authentication failed: {e}")
                return False

    def _authenticate_sync(self) -> bool:
        """Synchronous authentication (run in thread pool)."""
        try:
            # Check for existing token
            if os.path.exists(self.config.token_path):
                self._creds = Credentials.from_authorized_user_file(
                    self.config.token_path, GOOGLE_WORKSPACE_SCOPES
                )

            # Refresh or get new credentials
            if not self._creds or not self._creds.valid:
                if self._creds and self._creds.expired and self._creds.refresh_token:
                    logger.info("Refreshing Google OAuth token...")
                    self._creds.refresh(Request())
                else:
                    if not os.path.exists(self.config.credentials_path):
                        logger.error(
                            f"Google credentials file not found: {self.config.credentials_path}"
                        )
                        return False

                    logger.info("Starting OAuth flow for Google Workspace...")
                    flow = InstalledAppFlow.from_client_secrets_file(
                        self.config.credentials_path, GOOGLE_WORKSPACE_SCOPES
                    )
                    self._creds = flow.run_local_server(port=0)

                # Save token
                os.makedirs(os.path.dirname(self.config.token_path), exist_ok=True)
                with open(self.config.token_path, 'w') as token:
                    token.write(self._creds.to_json())

            # Build services
            self._gmail_service = build('gmail', 'v1', credentials=self._creds)
            self._calendar_service = build('calendar', 'v3', credentials=self._creds)
            self._people_service = build('people', 'v1', credentials=self._creds)

            logger.info("Google Workspace APIs authenticated successfully")
            return True

        except Exception as e:
            logger.exception(f"Sync authentication failed: {e}")
            return False

    async def _ensure_authenticated(self) -> bool:
        """Ensure client is authenticated."""
        if not self._authenticated:
            return await self.authenticate()
        return True

    def _get_cached(self, key: str) -> Optional[Any]:
        """Get cached value if not expired."""
        if key in self._cache:
            value, timestamp = self._cache[key]
            if (datetime.now().timestamp() - timestamp) < self.config.cache_ttl_seconds:
                return value
            del self._cache[key]
        return None

    def _set_cached(self, key: str, value: Any) -> None:
        """Cache a value."""
        self._cache[key] = (value, datetime.now().timestamp())

    # =========================================================================
    # Gmail Operations
    # =========================================================================

    async def fetch_unread_emails(
        self,
        limit: int = 10,
        label: str = "INBOX",
    ) -> Dict[str, Any]:
        """
        Fetch unread emails.

        Args:
            limit: Maximum number of emails to fetch
            label: Label to filter by

        Returns:
            Dictionary with email list and metadata
        """
        if not await self._ensure_authenticated():
            return {"error": "Not authenticated", "emails": []}

        cache_key = f"unread:{label}:{limit}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: self._fetch_unread_sync(limit, label)
            )
            self._set_cached(cache_key, result)
            return result

        except Exception as e:
            logger.exception(f"Error fetching emails: {e}")
            return {"error": str(e), "emails": []}

    def _fetch_unread_sync(self, limit: int, label: str) -> Dict[str, Any]:
        """Synchronous email fetch."""
        results = self._gmail_service.users().messages().list(
            userId='me',
            labelIds=[label, 'UNREAD'],
            maxResults=limit,
        ).execute()

        messages = results.get('messages', [])
        emails = []

        for msg_data in messages:
            msg = self._gmail_service.users().messages().get(
                userId='me',
                id=msg_data['id'],
                format='metadata',
                metadataHeaders=['From', 'To', 'Subject', 'Date'],
            ).execute()

            headers = {h['name']: h['value'] for h in msg.get('payload', {}).get('headers', [])}

            emails.append({
                "id": msg['id'],
                "thread_id": msg['threadId'],
                "from": headers.get('From', 'Unknown'),
                "to": headers.get('To', ''),
                "subject": headers.get('Subject', '(no subject)'),
                "date": headers.get('Date', ''),
                "snippet": msg.get('snippet', '')[:self.config.max_email_body_preview],
                "labels": msg.get('labelIds', []),
            })

        return {
            "emails": emails,
            "count": len(emails),
            "total_unread": results.get('resultSizeEstimate', 0),
        }

    async def search_emails(
        self,
        query: str,
        limit: int = 10,
    ) -> Dict[str, Any]:
        """
        Search emails with Gmail query syntax.

        Args:
            query: Gmail search query
            limit: Maximum results

        Returns:
            Search results
        """
        if not await self._ensure_authenticated():
            return {"error": "Not authenticated", "emails": []}

        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                lambda: self._search_emails_sync(query, limit)
            )
        except Exception as e:
            logger.exception(f"Error searching emails: {e}")
            return {"error": str(e), "emails": []}

    def _search_emails_sync(self, query: str, limit: int) -> Dict[str, Any]:
        """Synchronous email search."""
        results = self._gmail_service.users().messages().list(
            userId='me',
            q=query,
            maxResults=limit,
        ).execute()

        messages = results.get('messages', [])
        emails = []

        for msg_data in messages:
            msg = self._gmail_service.users().messages().get(
                userId='me',
                id=msg_data['id'],
                format='metadata',
                metadataHeaders=['From', 'To', 'Subject', 'Date'],
            ).execute()

            headers = {h['name']: h['value'] for h in msg.get('payload', {}).get('headers', [])}

            emails.append({
                "id": msg['id'],
                "from": headers.get('From', 'Unknown'),
                "subject": headers.get('Subject', '(no subject)'),
                "date": headers.get('Date', ''),
                "snippet": msg.get('snippet', '')[:self.config.max_email_body_preview],
            })

        return {
            "emails": emails,
            "count": len(emails),
            "query": query,
        }

    async def draft_email(
        self,
        to: str,
        subject: str,
        body: str,
        reply_to_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create an email draft.

        Args:
            to: Recipient email
            subject: Email subject
            body: Email body
            reply_to_id: Optional message ID to reply to

        Returns:
            Draft info
        """
        if not await self._ensure_authenticated():
            return {"error": "Not authenticated"}

        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                lambda: self._draft_email_sync(to, subject, body, reply_to_id)
            )
        except Exception as e:
            logger.exception(f"Error creating draft: {e}")
            return {"error": str(e)}

    def _draft_email_sync(
        self,
        to: str,
        subject: str,
        body: str,
        reply_to_id: Optional[str],
    ) -> Dict[str, Any]:
        """Synchronous draft creation."""
        message = MIMEMultipart()
        message['to'] = to
        message['subject'] = subject
        message.attach(MIMEText(body, 'plain'))

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')

        draft_body = {'message': {'raw': raw}}
        if reply_to_id:
            draft_body['message']['threadId'] = reply_to_id

        draft = self._gmail_service.users().drafts().create(
            userId='me',
            body=draft_body,
        ).execute()

        return {
            "status": "created",
            "draft_id": draft['id'],
            "message_id": draft['message']['id'],
            "to": to,
            "subject": subject,
        }

    async def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        html_body: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Send an email.

        Args:
            to: Recipient email
            subject: Email subject
            body: Plain text body
            html_body: Optional HTML body

        Returns:
            Send result
        """
        if not await self._ensure_authenticated():
            return {"error": "Not authenticated"}

        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                lambda: self._send_email_sync(to, subject, body, html_body)
            )
        except Exception as e:
            logger.exception(f"Error sending email: {e}")
            return {"error": str(e)}

    def _send_email_sync(
        self,
        to: str,
        subject: str,
        body: str,
        html_body: Optional[str],
    ) -> Dict[str, Any]:
        """Synchronous email send."""
        if html_body:
            message = MIMEMultipart('alternative')
            message['to'] = to
            message['subject'] = subject
            message.attach(MIMEText(body, 'plain'))
            message.attach(MIMEText(html_body, 'html'))
        else:
            message = MIMEText(body, 'plain')
            message['to'] = to
            message['subject'] = subject

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')

        result = self._gmail_service.users().messages().send(
            userId='me',
            body={'raw': raw},
        ).execute()

        return {
            "status": "sent",
            "message_id": result['id'],
            "thread_id": result.get('threadId'),
            "to": to,
            "subject": subject,
        }

    # =========================================================================
    # Calendar Operations
    # =========================================================================

    async def get_calendar_events(
        self,
        date_str: Optional[str] = None,
        days: int = 1,
    ) -> Dict[str, Any]:
        """
        Get calendar events for a date range.

        Args:
            date_str: Start date (ISO format) or None for today
            days: Number of days to look ahead

        Returns:
            Events data
        """
        if not await self._ensure_authenticated():
            return {"error": "Not authenticated", "events": []}

        # Parse date
        if date_str:
            try:
                start_date = datetime.fromisoformat(date_str)
            except ValueError:
                start_date = datetime.now()
        else:
            start_date = datetime.now()

        # Set time bounds
        time_min = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
        time_max = time_min + timedelta(days=days)

        cache_key = f"calendar:{time_min.isoformat()}:{days}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: self._get_events_sync(time_min, time_max)
            )
            self._set_cached(cache_key, result)
            return result

        except Exception as e:
            logger.exception(f"Error fetching calendar: {e}")
            return {"error": str(e), "events": []}

    def _get_events_sync(
        self,
        time_min: datetime,
        time_max: datetime,
    ) -> Dict[str, Any]:
        """Synchronous calendar fetch."""
        events_result = self._calendar_service.events().list(
            calendarId='primary',
            timeMin=time_min.isoformat() + 'Z',
            timeMax=time_max.isoformat() + 'Z',
            singleEvents=True,
            orderBy='startTime',
        ).execute()

        events = events_result.get('items', [])
        formatted_events = []

        for event in events:
            start = event.get('start', {})
            end = event.get('end', {})

            formatted_events.append({
                "id": event.get('id'),
                "title": event.get('summary', '(No title)'),
                "description": event.get('description', ''),
                "location": event.get('location', ''),
                "start": start.get('dateTime') or start.get('date'),
                "end": end.get('dateTime') or end.get('date'),
                "is_all_day": 'date' in start and 'dateTime' not in start,
                "attendees": [
                    {
                        "email": a.get('email'),
                        "name": a.get('displayName'),
                        "response": a.get('responseStatus'),
                    }
                    for a in event.get('attendees', [])
                ],
                "meeting_link": event.get('hangoutLink'),
                "status": event.get('status'),
            })

        return {
            "events": formatted_events,
            "count": len(formatted_events),
            "date_range": {
                "start": time_min.isoformat(),
                "end": time_max.isoformat(),
            },
        }

    async def create_calendar_event(
        self,
        title: str,
        start: str,
        end: Optional[str] = None,
        description: str = "",
        location: str = "",
        attendees: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Create a calendar event.

        Args:
            title: Event title
            start: Start time (ISO format)
            end: End time (ISO format) or None for default duration
            description: Event description
            location: Event location
            attendees: List of attendee emails

        Returns:
            Created event info
        """
        if not await self._ensure_authenticated():
            return {"error": "Not authenticated"}

        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                lambda: self._create_event_sync(
                    title, start, end, description, location, attendees
                )
            )
        except Exception as e:
            logger.exception(f"Error creating event: {e}")
            return {"error": str(e)}

    def _create_event_sync(
        self,
        title: str,
        start: str,
        end: Optional[str],
        description: str,
        location: str,
        attendees: Optional[List[str]],
    ) -> Dict[str, Any]:
        """Synchronous event creation."""
        # Parse start time
        start_dt = datetime.fromisoformat(start.replace('Z', '+00:00'))

        # Calculate end time if not provided
        if end:
            end_dt = datetime.fromisoformat(end.replace('Z', '+00:00'))
        else:
            end_dt = start_dt + timedelta(minutes=self.config.default_event_duration_minutes)

        event_body = {
            'summary': title,
            'description': description,
            'location': location,
            'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'America/Los_Angeles'},
            'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'America/Los_Angeles'},
        }

        if attendees:
            event_body['attendees'] = [{'email': email} for email in attendees]

        event = self._calendar_service.events().insert(
            calendarId='primary',
            body=event_body,
        ).execute()

        return {
            "status": "created",
            "event_id": event.get('id'),
            "title": title,
            "start": start,
            "end": end_dt.isoformat(),
            "link": event.get('htmlLink'),
        }

    # =========================================================================
    # Contacts Operations
    # =========================================================================

    async def get_contacts(
        self,
        query: Optional[str] = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """
        Get contacts, optionally filtered by query.

        Args:
            query: Optional search query
            limit: Maximum results

        Returns:
            Contacts data
        """
        if not await self._ensure_authenticated():
            return {"error": "Not authenticated", "contacts": []}

        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                lambda: self._get_contacts_sync(query, limit)
            )
        except Exception as e:
            logger.exception(f"Error fetching contacts: {e}")
            return {"error": str(e), "contacts": []}

    def _get_contacts_sync(
        self,
        query: Optional[str],
        limit: int,
    ) -> Dict[str, Any]:
        """Synchronous contacts fetch."""
        # Use the connections API
        results = self._people_service.people().connections().list(
            resourceName='people/me',
            pageSize=limit,
            personFields='names,emailAddresses,phoneNumbers,organizations',
        ).execute()

        connections = results.get('connections', [])
        contacts = []

        for person in connections:
            names = person.get('names', [{}])
            emails = person.get('emailAddresses', [])
            phones = person.get('phoneNumbers', [])
            orgs = person.get('organizations', [])

            name = names[0].get('displayName', '') if names else ''

            # Filter by query if provided
            if query:
                query_lower = query.lower()
                if query_lower not in name.lower():
                    email_match = any(
                        query_lower in e.get('value', '').lower()
                        for e in emails
                    )
                    if not email_match:
                        continue

            contacts.append({
                "name": name,
                "emails": [e.get('value') for e in emails if e.get('value')],
                "phones": [p.get('value') for p in phones if p.get('value')],
                "organization": orgs[0].get('name') if orgs else None,
            })

        return {
            "contacts": contacts,
            "count": len(contacts),
        }


# =============================================================================
# Google Workspace Agent
# =============================================================================

class GoogleWorkspaceAgent(BaseNeuralMeshAgent):
    """
    Google Workspace Agent - "Chief of Staff" for Admin & Communication.

    This agent handles all Google Workspace operations including:
    - Gmail (read, send, draft, search)
    - Calendar (view, create events)
    - Contacts (lookup)
    - Google Docs (create documents with AI content)

    **UNIFIED EXECUTION ARCHITECTURE**

    This agent implements a "Never-Fail" waterfall strategy:
    - Tier 1: Google API (fast, cloud-based)
    - Tier 2: macOS Local (CalendarBridge, native apps)
    - Tier 3: Computer Use (visual automation)

    Even if Google APIs are unavailable, JARVIS can still check your
    calendar by opening the Calendar app and reading it visually.

    Usage:
        agent = GoogleWorkspaceAgent()
        await coordinator.register_agent(agent)

        # The agent will automatically handle workspace queries
        result = await agent.execute_task({
            "action": "check_calendar_events",
            "date": "today",
        })
    """

    def __init__(self, config: Optional[GoogleWorkspaceConfig] = None) -> None:
        """Initialize the Google Workspace Agent."""
        super().__init__(
            agent_name="google_workspace_agent",
            agent_type="admin",  # Admin/Communication agent type
            capabilities={
                # Email capabilities
                "fetch_unread_emails",
                "search_email",
                "draft_email_reply",
                "send_email",
                # Calendar capabilities
                "check_calendar_events",
                "create_calendar_event",
                "find_free_time",
                # Contacts
                "get_contacts",
                # Composite
                "workspace_summary",
                "daily_briefing",
                # Document creation
                "create_document",
                # Routing
                "handle_workspace_query",
            },
            version="2.0.0",  # Unified Execution version
        )

        self.config = config or GoogleWorkspaceConfig()
        self._client: Optional[GoogleWorkspaceClient] = None
        self._intent_detector = WorkspaceIntentDetector()

        # Unified Executor for "Never-Fail" waterfall strategy
        self._unified_executor: Optional[UnifiedWorkspaceExecutor] = None

        # Statistics
        self._email_queries = 0
        self._calendar_queries = 0
        self._emails_sent = 0
        self._drafts_created = 0
        self._events_created = 0
        self._documents_created = 0
        self._fallback_uses = 0

    async def on_initialize(self) -> None:
        """Initialize agent resources."""
        logger.info("Initializing GoogleWorkspaceAgent v2.0 (Unified Execution)")

        # Create Google API client (lazy authentication)
        self._client = GoogleWorkspaceClient(self.config)

        # Initialize Unified Executor for waterfall fallbacks
        self._unified_executor = UnifiedWorkspaceExecutor()
        await self._unified_executor.initialize()
        logger.info(
            f"Unified Executor ready: {self._unified_executor.get_stats()['available_tiers']}"
        )

        # Subscribe to workspace-related messages
        await self.subscribe(
            MessageType.CUSTOM,
            self._handle_workspace_message,
        )

        logger.info("GoogleWorkspaceAgent initialized with Never-Fail fallbacks")

    async def on_start(self) -> None:
        """Called when agent starts."""
        logger.info("GoogleWorkspaceAgent started - ready for workspace operations")

        # Optionally authenticate on start
        # await self._ensure_client()

    async def on_stop(self) -> None:
        """Cleanup when agent stops."""
        logger.info(
            f"GoogleWorkspaceAgent stopping - processed "
            f"{self._email_queries} email queries, "
            f"{self._calendar_queries} calendar queries, "
            f"{self._emails_sent} emails sent, "
            f"{self._events_created} events created"
        )

    async def _ensure_client(self) -> bool:
        """Ensure client is authenticated."""
        if self._client is None:
            self._client = GoogleWorkspaceClient(self.config)
        return await self._client.authenticate()

    # =========================================================================
    # v3.0: Trinity Loop Integration - Visual Context & Experience Logging
    # =========================================================================

    async def _resolve_entities_from_visual_context(
        self,
        query: str,
        visual_context: Optional[str],
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Resolve ambiguous entities ("this", "him/her") from visual context.

        When the user says "Reply to this email" or "Email him", we use
        OCR text from the screen to determine who/what they mean.

        Args:
            query: The user's original query
            visual_context: OCR text from the current screen
            payload: Original payload to enrich with resolved entities

        Returns:
            Enriched payload with resolved entities
        """
        if not visual_context:
            return payload

        # Check for ambiguous references
        ambiguous_patterns = [
            "this email", "this message", "this",
            "him", "her", "them", "the sender",
            "that person", "this person", "reply to him",
            "email him", "email her", "message him",
        ]

        query_lower = query.lower()
        needs_resolution = any(pattern in query_lower for pattern in ambiguous_patterns)

        if not needs_resolution:
            return payload

        logger.info("[GoogleWorkspaceAgent] Resolving entities from visual context...")

        # Use LLM to extract entities from visual context
        if UNIFIED_MODEL_SERVING_AVAILABLE and get_model_serving:
            try:
                model_serving = await get_model_serving()

                extraction_prompt = f"""You are an entity extraction assistant. Analyze the screen text and user query to extract relevant information.

SCREEN TEXT (OCR):
{visual_context[:2000]}

USER QUERY: {query}

Extract the following if present:
1. Email sender name and email address (look for "From:" patterns)
2. Email subject (look for "Subject:" patterns)
3. Email recipient if mentioned
4. Any names mentioned
5. Any other relevant context for the query

Return ONLY a JSON object with these keys (use null if not found):
{{
    "sender_name": "...",
    "sender_email": "...",
    "subject": "...",
    "recipient_name": "...",
    "recipient_email": "...",
    "context_summary": "..."
}}"""

                result = await model_serving.generate(
                    prompt=extraction_prompt,
                    task_type="analysis",
                    max_tokens=500,
                )

                if result.get("text"):
                    import json
                    try:
                        # Parse JSON from response
                        response_text = result["text"]
                        # Find JSON in response
                        json_start = response_text.find("{")
                        json_end = response_text.rfind("}") + 1
                        if json_start >= 0 and json_end > json_start:
                            extracted = json.loads(response_text[json_start:json_end])

                            # Enrich payload with extracted entities
                            if extracted.get("sender_email") and not payload.get("to"):
                                payload["to"] = extracted["sender_email"]
                                payload["resolved_from_visual"] = True
                                logger.info(
                                    f"[GoogleWorkspaceAgent] Resolved recipient: {extracted['sender_email']}"
                                )

                            if extracted.get("sender_name"):
                                payload["resolved_name"] = extracted["sender_name"]

                            if extracted.get("subject") and not payload.get("subject"):
                                # For replies, prepend "Re:" if not present
                                subject = extracted["subject"]
                                if not subject.lower().startswith("re:"):
                                    subject = f"Re: {subject}"
                                payload["subject"] = subject

                            if extracted.get("context_summary"):
                                payload["visual_context_summary"] = extracted["context_summary"]

                    except json.JSONDecodeError:
                        logger.warning("Failed to parse entity extraction response as JSON")

            except Exception as e:
                logger.warning(f"Entity resolution failed: {e}")

        return payload

    async def _log_experience(
        self,
        action: str,
        input_data: Dict[str, Any],
        output_data: Dict[str, Any],
        success: bool = True,
        confidence: float = 0.9,
        visual_context: Optional[str] = None,
    ) -> None:
        """
        Log an experience to Reactor Core for training.

        This enables the Trinity Loop - JARVIS learns from every interaction
        and improves over time through Reactor Core's training pipeline.

        Args:
            action: The action performed (e.g., "send_email", "check_calendar")
            input_data: Input parameters (sanitized for privacy)
            output_data: Output/result of the action
            success: Whether the action succeeded
            confidence: Confidence score for this experience
            visual_context: OCR context used (for learning visual patterns)
        """
        if not EXPERIENCE_FORWARDER_AVAILABLE or not get_experience_forwarder:
            return

        try:
            forwarder = await get_experience_forwarder()

            # Sanitize input for privacy (remove sensitive PII)
            sanitized_input = self._sanitize_for_logging(input_data)
            sanitized_output = self._sanitize_for_logging(output_data)

            # Build experience metadata
            metadata = {
                "agent": "google_workspace_agent",
                "agent_version": "3.0.0",
                "action": action,
                "tier_used": output_data.get("tier_used", "google_api"),
                "execution_time_ms": output_data.get("execution_time_ms", 0),
                "had_visual_context": visual_context is not None,
            }

            # Forward to Reactor Core
            await forwarder.forward_experience(
                experience_type=f"workspace_{action}",
                input_data={
                    "action": action,
                    "query": sanitized_input.get("query", ""),
                    "parameters": sanitized_input,
                },
                output_data={
                    "success": success,
                    "result_summary": self._summarize_result(sanitized_output),
                },
                quality_score=0.8 if success else 0.3,
                confidence=confidence,
                success=success,
                component="google_workspace_agent",
                metadata=metadata,
            )

            logger.debug(f"[GoogleWorkspaceAgent] Logged experience for action: {action}")

        except Exception as e:
            # Don't fail the main operation if logging fails
            logger.debug(f"[GoogleWorkspaceAgent] Failed to log experience: {e}")

    def _sanitize_for_logging(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Sanitize data for logging by removing sensitive PII.

        Strips email addresses, full names, and other identifying info
        while preserving structure for training.
        """
        if not isinstance(data, dict):
            return data

        sanitized = {}
        sensitive_keys = {"body", "html_body", "content", "password", "token", "key"}
        pii_keys = {"to", "from", "email", "phone", "address"}

        for key, value in data.items():
            key_lower = key.lower()

            if key_lower in sensitive_keys:
                # Redact completely
                sanitized[key] = "[REDACTED]"
            elif key_lower in pii_keys:
                # Hash for deduplication but don't expose
                if isinstance(value, str) and "@" in value:
                    # Hash email domain only
                    parts = value.split("@")
                    if len(parts) == 2:
                        sanitized[key] = f"***@{parts[1]}"
                    else:
                        sanitized[key] = "[EMAIL]"
                else:
                    sanitized[key] = "[PII]"
            elif isinstance(value, dict):
                sanitized[key] = self._sanitize_for_logging(value)
            elif isinstance(value, list):
                sanitized[key] = [
                    self._sanitize_for_logging(item) if isinstance(item, dict) else item
                    for item in value
                ]
            else:
                sanitized[key] = value

        return sanitized

    def _summarize_result(self, result: Dict[str, Any]) -> str:
        """Create a short summary of a result for logging."""
        if result.get("error"):
            return f"Error: {result['error'][:100]}"

        summaries = []
        if "count" in result:
            summaries.append(f"count={result['count']}")
        if "status" in result:
            summaries.append(f"status={result['status']}")
        if "tier_used" in result:
            summaries.append(f"tier={result['tier_used']}")

        return ", ".join(summaries) if summaries else "success"

    async def execute_task(self, payload: Dict[str, Any]) -> Any:
        """
        Execute a workspace task.

        Supported actions:
        - fetch_unread_emails: Get unread emails (with fallback)
        - search_email: Search emails
        - draft_email_reply: Create email draft
        - send_email: Send an email
        - check_calendar_events: Get calendar events (with fallback)
        - create_calendar_event: Create a calendar event
        - create_document: Create Google Doc with AI content
        - get_contacts: Get contacts
        - workspace_summary: Get daily briefing
        - handle_workspace_query: Natural language query handler
        - read_spreadsheet: Read data from Google Sheets
        - write_spreadsheet: Write data to Google Sheets

        Note: Actions with "(with fallback)" use the unified executor
        and will try alternative methods if the primary fails.

        v10.0 Enhancement - Visual Execution Mode ("Iron Man" Experience):
        If payload contains execution_mode="visual_preferred" or "visual_only",
        interactive commands (draft_email_reply, create_document) will use
        Computer Use (Tier 3) directly for visible on-screen execution.

        v3.0 Enhancement - Trinity Loop Integration:
        - visual_context: OCR text from screen for entity resolution
        - Automatic experience logging to Reactor Core
        """
        action = payload.get("action", "")
        execution_mode = payload.get("execution_mode", "auto")
        visual_context = payload.get("visual_context")
        query = payload.get("query", "")

        logger.debug(f"GoogleWorkspaceAgent executing: {action}")

        # v3.0: Resolve entities from visual context if present
        if visual_context and query:
            payload = await self._resolve_entities_from_visual_context(
                query=query,
                visual_context=visual_context,
                payload=payload,
            )

        # Actions that support fallback don't require authentication
        fallback_actions = {
            "fetch_unread_emails",
            "check_calendar_events",
            "create_document",
            "handle_workspace_query",
            "workspace_summary",
            "daily_briefing",
        }

        # For non-fallback actions, try to authenticate (but don't fail hard)
        if action not in fallback_actions:
            auth_success = await self._ensure_client()
            if not auth_success:
                logger.warning(
                    f"Google API auth failed for {action}, but proceeding "
                    f"(some operations may fail)"
                )

        # Route to appropriate handler
        if action == "fetch_unread_emails":
            return await self._fetch_unread_emails(payload)
        elif action == "search_email":
            return await self._search_email(payload)
        elif action == "draft_email_reply":
            # v10.0: Check for visual execution mode preference
            if execution_mode in ("visual_preferred", "visual_only"):
                return await self._draft_email_visual(payload)
            return await self._draft_email(payload)
        elif action == "send_email":
            return await self._send_email(payload)
        elif action == "check_calendar_events":
            return await self._check_calendar(payload)
        elif action == "create_calendar_event":
            return await self._create_event(payload)
        elif action == "create_document":
            return await self._create_document(payload)
        elif action == "get_contacts":
            return await self._get_contacts(payload)
        elif action == "workspace_summary":
            return await self._get_workspace_summary(payload)
        elif action == "daily_briefing":
            return await self._get_workspace_summary(payload)
        elif action == "handle_workspace_query":
            return await self._handle_natural_query(payload)
        elif action == "read_spreadsheet":
            return await self._read_spreadsheet(payload)
        elif action == "write_spreadsheet":
            return await self._write_spreadsheet(payload)
        else:
            raise ValueError(f"Unknown workspace action: {action}")

    async def _fetch_unread_emails(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Fetch unread emails using unified executor with waterfall fallback.

        Tries:
        1. Gmail API (if authenticated)
        2. Computer Use (visual - open Gmail in browser)
        """
        limit = payload.get("limit", self.config.default_email_limit)

        self._email_queries += 1

        # Use unified executor for waterfall fallback
        if self._unified_executor:
            exec_result = await self._unified_executor.execute_email_check(
                google_client=self._client if self._client else None,
                limit=limit,
            )

            if exec_result.success:
                result = exec_result.data
                result["tier_used"] = exec_result.tier_used.value
                result["execution_time_ms"] = exec_result.execution_time_ms

                if exec_result.fallback_attempted:
                    self._fallback_uses += 1
                    logger.info(
                        f"Email check succeeded via fallback: {exec_result.tier_used.value}"
                    )

                # Add to knowledge graph
                if self.knowledge_graph:
                    await self.add_knowledge(
                        knowledge_type=KnowledgeType.OBSERVATION,
                        data={
                            "type": "email_check",
                            "unread_count": result.get("count", 0),
                            "tier_used": exec_result.tier_used.value,
                            "checked_at": datetime.now().isoformat(),
                        },
                        confidence=1.0,
                    )

                # v3.1: Log experience to Reactor Core
                await self._log_experience(
                    action="fetch_unread_emails",
                    input_data={"limit": limit},
                    output_data={
                        "email_count": result.get("count", 0),
                        "tier_used": exec_result.tier_used.value,
                        "execution_time_ms": exec_result.execution_time_ms,
                    },
                    success=True,
                    confidence=0.9,
                )

                return result
            else:
                return {
                    "error": exec_result.error or "All email check methods failed",
                    "emails": [],
                }

        # Fallback to direct client call if executor not available
        if self._client:
            return await self._client.fetch_unread_emails(limit=limit)

        return {"error": "No execution method available", "emails": []}

    async def _search_email(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Search emails."""
        query = payload.get("query", "")
        limit = payload.get("limit", self.config.default_email_limit)

        self._email_queries += 1

        return await self._client.search_emails(query=query, limit=limit)

    async def _draft_email(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create email draft with intelligent Prime model generation.

        v3.1 Enhancement - Trinity Loop Integration:
        - Uses Prime models for email body generation when not provided
        - Routes to appropriate model based on complexity (fast vs reasoning)
        - Logs experience to Reactor Core for learning
        - Tracks user edits for preference learning

        Args:
            payload: Dict with:
                - to: Recipient email
                - subject: Email subject
                - body: Optional email body (generated if not provided)
                - reply_to_id: Optional ID of email being replied to
                - query: Original user query for context
                - visual_context: Optional OCR context
                - tone: Optional tone preference (professional, casual, friendly)
                - original_email_content: Content of email being replied to

        Returns:
            Dict with draft status and metadata
        """
        import time as time_module
        start_time = time_module.time()

        to = payload.get("to", "")
        subject = payload.get("subject", "")
        body = payload.get("body", "")
        reply_to = payload.get("reply_to_id")
        query = payload.get("query", "")
        visual_context = payload.get("visual_context")
        tone = payload.get("tone", "professional")
        original_email = payload.get("original_email_content", "")

        if not to:
            return {"error": "Recipient 'to' is required", "success": False}
        if not subject:
            return {"error": "Subject is required", "success": False}

        generated_body = False
        model_used = None
        generation_time_ms = 0

        # v3.1: Generate email body using Prime models if not provided
        if not body and (query or original_email):
            generation_start = time_module.time()

            if UNIFIED_MODEL_SERVING_AVAILABLE and get_model_serving:
                try:
                    model_serving = await get_model_serving()

                    # Build intelligent email generation prompt
                    email_context = ""
                    if original_email:
                        email_context = f"\n\nORIGINAL EMAIL TO REPLY TO:\n{original_email[:1500]}"
                    if visual_context:
                        email_context += f"\n\nSCREEN CONTEXT:\n{visual_context[:500]}"

                    email_prompt = f"""You are an email drafting assistant. Generate a {tone} email reply.

RECIPIENT: {to}
SUBJECT: {subject}
USER REQUEST: {query or "Draft a reply to this email"}
{email_context}

Generate ONLY the email body (no subject, no greeting like "Dear", just the content).
The email should:
1. Be {tone} in tone
2. Be concise but complete
3. Address the key points from the original email if replying
4. End with an appropriate sign-off

EMAIL BODY:"""

                    # Use workspace_email task type for routing
                    result = await model_serving.generate(
                        prompt=email_prompt,
                        task_type="workspace_email",
                        max_tokens=800,
                    )

                    if result.get("text"):
                        body = result["text"].strip()
                        generated_body = True
                        model_used = result.get("provider", "prime")
                        generation_time_ms = (time_module.time() - generation_start) * 1000

                        logger.info(
                            f"[GoogleWorkspaceAgent] Generated email body using {model_used} "
                            f"({generation_time_ms:.0f}ms)"
                        )

                except Exception as e:
                    logger.warning(f"Prime email generation failed: {e}")

        if not body:
            return {"error": "Email body is required (generation failed)", "success": False}

        # Create draft via Gmail API
        result = await self._client.draft_email(
            to=to,
            subject=subject,
            body=body,
            reply_to_id=reply_to,
        )

        execution_time_ms = (time_module.time() - start_time) * 1000

        if result.get("status") == "created":
            self._drafts_created += 1

            # v3.1: Store draft for user edit tracking
            draft_id = result.get("draft_id", result.get("id", ""))
            if draft_id:
                await self._track_draft_for_edits(
                    draft_id=draft_id,
                    original_body=body,
                    generated=generated_body,
                    model_used=model_used,
                    query=query,
                )

            # v3.1: Log experience to Reactor Core
            await self._log_experience(
                action="draft_email",
                input_data={
                    "query": query,
                    "tone": tone,
                    "has_original_email": bool(original_email),
                    "had_visual_context": bool(visual_context),
                },
                output_data={
                    **result,
                    "generated_body": generated_body,
                    "model_used": model_used,
                    "generation_time_ms": generation_time_ms,
                    "execution_time_ms": execution_time_ms,
                },
                success=True,
                confidence=0.9 if generated_body else 0.95,
            )

        # Add metadata to result
        result["generated_body"] = generated_body
        result["model_used"] = model_used
        result["execution_time_ms"] = execution_time_ms

        return result

    async def _track_draft_for_edits(
        self,
        draft_id: str,
        original_body: str,
        generated: bool,
        model_used: Optional[str],
        query: str,
    ) -> None:
        """
        Track a draft for user edit learning.

        v3.1: When user edits a generated draft, we learn their preferences.
        This is part of the Trinity Loop - corrections improve future generations.
        """
        try:
            # Store in memory for short-term tracking
            if not hasattr(self, "_draft_tracking"):
                self._draft_tracking: Dict[str, Dict[str, Any]] = {}

            self._draft_tracking[draft_id] = {
                "original_body": original_body,
                "generated": generated,
                "model_used": model_used,
                "query": query,
                "created_at": asyncio.get_event_loop().time(),
            }

            # Limit tracked drafts to prevent memory bloat
            if len(self._draft_tracking) > 50:
                # Remove oldest entries
                sorted_drafts = sorted(
                    self._draft_tracking.items(),
                    key=lambda x: x[1]["created_at"],
                )
                for draft_id_old, _ in sorted_drafts[:10]:
                    del self._draft_tracking[draft_id_old]

        except Exception as e:
            logger.debug(f"Draft tracking failed: {e}")

    async def check_draft_for_user_edits(self, draft_id: str) -> Optional[Dict[str, Any]]:
        """
        Check if user edited a tracked draft and learn from changes.

        Call this when a draft is sent to capture user corrections.
        Returns edit analysis if the draft was tracked and edited.
        """
        if not hasattr(self, "_draft_tracking"):
            return None

        tracked = self._draft_tracking.get(draft_id)
        if not tracked:
            return None

        try:
            # Fetch current draft content
            if self._client:
                current_draft = await self._client.get_draft(draft_id)
                if current_draft and current_draft.get("body"):
                    current_body = current_draft["body"]
                    original_body = tracked["original_body"]

                    # Calculate edit distance / changes
                    if current_body != original_body:
                        # User made edits - this is valuable learning data
                        edit_data = {
                            "draft_id": draft_id,
                            "original_body": original_body,
                            "edited_body": current_body,
                            "generated": tracked["generated"],
                            "model_used": tracked["model_used"],
                            "query": tracked["query"],
                            "edit_detected": True,
                        }

                        # Log as correction experience
                        await self._log_experience(
                            action="draft_correction",
                            input_data={
                                "query": tracked["query"],
                                "original": original_body[:500],
                            },
                            output_data={
                                "corrected": current_body[:500],
                                "model_used": tracked["model_used"],
                            },
                            success=True,
                            confidence=1.0,  # User corrections are high quality
                        )

                        # Clean up tracking
                        del self._draft_tracking[draft_id]

                        return edit_data

        except Exception as e:
            logger.debug(f"Edit check failed: {e}")

        return None

    async def _draft_email_visual(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        v10.0: Draft email using Computer Use (visual execution).

        This provides the "Iron Man" experience - JARVIS physically switches
        to Gmail and types the email visibly on screen using spatial awareness
        and Computer Use.

        Args:
            payload: Dict with:
                - to: Recipient email/name
                - subject: Email subject
                - body: Email body (optional, can be generated)
                - spatial_target: Optional spatial target ("Gmail tab in Space 3")

        Returns:
            Execution result with visual tier info
        """
        start_time = asyncio.get_event_loop().time()

        to = payload.get("to", "")
        subject = payload.get("subject", "")
        body = payload.get("body", "")
        spatial_target = payload.get("spatial_target")

        logger.info(
            f"[GoogleWorkspaceAgent] 🎬 Drafting email VISUALLY "
            f"(to: {to}, subject: {subject[:30]}...)"
        )

        # Ensure unified executor is available
        if not self._unified_executor or not self._unified_executor._computer_use:
            logger.warning("Computer Use not available, falling back to API")
            return await self._draft_email(payload)

        try:
            # Step 1: Switch to Gmail using spatial awareness
            logger.info("[GoogleWorkspaceAgent] 🎯 Switching to Gmail via spatial awareness...")
            spatial_success = await self._unified_executor._switch_to_app_with_spatial_awareness(
                app_name="Safari",  # Assuming Gmail in browser
                narrate=True,
            )

            if not spatial_success:
                logger.warning("Failed to switch to Gmail, proceeding anyway")

            # Step 2: Use Computer Use to draft the email visually
            logger.info("[GoogleWorkspaceAgent] ⌨️  Drafting email via Computer Use...")

            # Build natural language goal for Computer Use
            goal = (
                f"Navigate to mail.google.com, click 'Compose' to start a new email, "
                f"and fill in the following:\n"
                f"- To: {to}\n"
                f"- Subject: {subject}\n"
            )

            if body:
                goal += f"- Body: {body}\n"
            else:
                goal += f"- Body: [Leave empty for user to write]\n"

            goal += (
                f"\n"
                f"DO NOT send the email - just create the draft and leave it open "
                f"for the user to review and edit."
            )

            # Execute via Computer Use
            result = await self._unified_executor._computer_use.run(goal=goal)

            execution_time_ms = (asyncio.get_event_loop().time() - start_time) * 1000

            if result and result.success:
                self._drafts_created += 1
                logger.info(
                    f"[GoogleWorkspaceAgent] ✅ Email drafted visually "
                    f"({execution_time_ms:.0f}ms, {result.actions_count} actions)"
                )

                return {
                    "success": True,
                    "status": "drafted_visually",
                    "tier_used": "computer_use",
                    "execution_mode": "visual",
                    "to": to,
                    "subject": subject,
                    "spatial_target": spatial_target,
                    "actions_count": result.actions_count,
                    "execution_time_ms": execution_time_ms,
                    "message": (
                        f"Email draft created visually on screen. "
                        f"Switched to Gmail and filled in recipient ({to}) and subject ({subject}). "
                        f"Draft is ready for you to review and edit."
                    ),
                }
            else:
                logger.warning("Computer Use failed for email draft, falling back to API")
                return await self._draft_email(payload)

        except Exception as e:
            logger.error(f"[GoogleWorkspaceAgent] Error in visual email draft: {e}")
            logger.info("Falling back to API for email draft")
            return await self._draft_email(payload)

    async def _send_email(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Send an email."""
        to = payload.get("to", "")
        subject = payload.get("subject", "")
        body = payload.get("body", "")
        html_body = payload.get("html_body")

        if not to:
            return {"error": "Recipient 'to' is required"}
        if not subject:
            return {"error": "Subject is required"}
        if not body:
            return {"error": "Email body is required"}

        result = await self._client.send_email(
            to=to,
            subject=subject,
            body=body,
            html_body=html_body,
        )

        if result.get("status") == "sent":
            self._emails_sent += 1

            # Record in knowledge graph
            if self.knowledge_graph:
                await self.add_knowledge(
                    knowledge_type=KnowledgeType.OBSERVATION,
                    data={
                        "type": "email_sent",
                        "to": to,
                        "subject": subject,
                        "sent_at": datetime.now().isoformat(),
                    },
                    confidence=1.0,
                )

            # v3.0: Log experience to Reactor Core
            await self._log_experience(
                action="send_email",
                input_data={"to": to, "subject": subject},
                output_data=result,
                success=True,
                confidence=0.95,
            )

        return result

    async def _check_calendar(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Check calendar events using unified executor with waterfall fallback.

        Tries:
        1. Google Calendar API (if authenticated)
        2. macOS CalendarBridge (native calendar)
        3. Computer Use (visual - open Calendar app)

        This is a "Never-Fail" operation - even if Google is down,
        JARVIS can still check your local calendar.
        """
        date_str = payload.get("date", "today")
        days = payload.get("days", 1)
        hours_ahead = days * 24

        # Handle relative dates for display
        display_date = date_str
        if date_str:
            date_lower = date_str.lower()
            if date_lower == "today":
                display_date = date.today().isoformat()
            elif date_lower == "tomorrow":
                display_date = (date.today() + timedelta(days=1)).isoformat()

        self._calendar_queries += 1

        # Use unified executor for waterfall fallback
        if self._unified_executor:
            exec_result = await self._unified_executor.execute_calendar_check(
                google_client=self._client if self._client else None,
                date_str=date_str,
                hours_ahead=hours_ahead,
            )

            if exec_result.success:
                result = exec_result.data
                result["tier_used"] = exec_result.tier_used.value
                result["execution_time_ms"] = exec_result.execution_time_ms
                result["date_queried"] = display_date

                if exec_result.fallback_attempted:
                    self._fallback_uses += 1
                    logger.info(
                        f"Calendar check succeeded via fallback: {exec_result.tier_used.value}"
                    )

                # Add observation to knowledge graph
                if self.knowledge_graph:
                    await self.add_knowledge(
                        knowledge_type=KnowledgeType.OBSERVATION,
                        data={
                            "type": "calendar_check",
                            "event_count": result.get("count", 0),
                            "tier_used": exec_result.tier_used.value,
                            "date_range": result.get("date_range"),
                            "checked_at": datetime.now().isoformat(),
                        },
                        confidence=1.0,
                    )

                # v3.1: Log experience to Reactor Core
                await self._log_experience(
                    action="check_calendar",
                    input_data={"date": date_str, "days": days},
                    output_data={
                        "event_count": result.get("count", 0),
                        "tier_used": exec_result.tier_used.value,
                        "execution_time_ms": exec_result.execution_time_ms,
                    },
                    success=True,
                    confidence=0.9,
                )

                return result
            else:
                return {
                    "error": exec_result.error or "All calendar check methods failed",
                    "events": [],
                    "count": 0,
                }

        # Fallback to direct client call if executor not available
        if self._client:
            return await self._client.get_calendar_events(date_str=display_date, days=days)

        return {"error": "No execution method available", "events": [], "count": 0}

    async def _create_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Create a calendar event."""
        title = payload.get("title", "")
        start = payload.get("start", "")
        end = payload.get("end")
        description = payload.get("description", "")
        location = payload.get("location", "")
        attendees = payload.get("attendees", [])

        if not title:
            return {"error": "Event title is required"}
        if not start:
            return {"error": "Start time is required"}

        result = await self._client.create_calendar_event(
            title=title,
            start=start,
            end=end,
            description=description,
            location=location,
            attendees=attendees,
        )

        if result.get("status") == "created":
            self._events_created += 1

            # v3.1: Log experience to Reactor Core
            await self._log_experience(
                action="create_event",
                input_data={
                    "has_title": bool(title),
                    "has_attendees": bool(attendees),
                    "has_location": bool(location),
                },
                output_data=result,
                success=True,
                confidence=0.95,
            )

        return result

    async def _get_contacts(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Get contacts."""
        query = payload.get("query")
        limit = payload.get("limit", 20)

        if self._client:
            return await self._client.get_contacts(query=query, limit=limit)
        return {"error": "Google API client not available", "contacts": []}

    async def _create_document(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create a Google Doc with AI-generated content.

        Uses unified executor with fallback:
        1. Google Docs API + Claude content generation
        2. Computer Use (open browser, create doc visually)

        Args:
            payload: Dict with:
                - topic: Subject/topic of the document
                - document_type: "essay", "report", "paper", etc.
                - word_count: Target word count (optional)
                - format: "mla", "apa", "chicago", etc. (optional)
        """
        topic = payload.get("topic", "")
        document_type = payload.get("document_type", "essay")
        word_count = payload.get("word_count")

        if not topic:
            return {"error": "Document topic is required"}

        logger.info(f"Creating document: {document_type} about '{topic}'")

        # Use unified executor for waterfall fallback
        if self._unified_executor:
            exec_result = await self._unified_executor.execute_document_creation(
                topic=topic,
                document_type=document_type,
                word_count=word_count,
            )

            if exec_result.success:
                self._documents_created += 1
                result = exec_result.data
                result["tier_used"] = exec_result.tier_used.value
                result["execution_time_ms"] = exec_result.execution_time_ms

                if exec_result.fallback_attempted:
                    self._fallback_uses += 1
                    logger.info(
                        f"Document creation succeeded via fallback: {exec_result.tier_used.value}"
                    )

                # Add to knowledge graph
                if self.knowledge_graph:
                    await self.add_knowledge(
                        knowledge_type=KnowledgeType.OBSERVATION,
                        data={
                            "type": "document_created",
                            "topic": topic,
                            "document_type": document_type,
                            "tier_used": exec_result.tier_used.value,
                            "created_at": datetime.now().isoformat(),
                        },
                        confidence=1.0,
                    )

                # v3.1: Log experience to Reactor Core
                await self._log_experience(
                    action="create_document",
                    input_data={
                        "document_type": document_type,
                        "has_word_count": bool(word_count),
                    },
                    output_data={
                        "tier_used": exec_result.tier_used.value,
                        "execution_time_ms": exec_result.execution_time_ms,
                    },
                    success=True,
                    confidence=0.9,
                )

                return result
            else:
                return {
                    "error": exec_result.error or "All document creation methods failed",
                    "success": False,
                }

        return {"error": "No execution method available for document creation", "success": False}

    async def _get_workspace_summary(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Get a comprehensive workspace summary (daily briefing).

        Returns summary of:
        - Unread emails
        - Today's calendar events
        - Upcoming deadlines
        """
        # Fetch in parallel
        email_task = self._client.fetch_unread_emails(limit=5)
        calendar_task = self._client.get_calendar_events(days=1)

        email_result, calendar_result = await asyncio.gather(
            email_task, calendar_task, return_exceptions=True
        )

        # Build summary
        summary = {
            "generated_at": datetime.now().isoformat(),
            "date": date.today().isoformat(),
        }

        # Email summary
        if isinstance(email_result, dict) and not email_result.get("error"):
            summary["email"] = {
                "unread_count": email_result.get("total_unread", 0),
                "recent_emails": [
                    {
                        "from": e.get("from"),
                        "subject": e.get("subject"),
                    }
                    for e in email_result.get("emails", [])[:3]
                ],
            }
        else:
            summary["email"] = {"error": str(email_result)}

        # Calendar summary
        if isinstance(calendar_result, dict) and not calendar_result.get("error"):
            events = calendar_result.get("events", [])
            summary["calendar"] = {
                "event_count": len(events),
                "events": [
                    {
                        "title": e.get("title"),
                        "start": e.get("start"),
                        "location": e.get("location"),
                    }
                    for e in events
                ],
            }
        else:
            summary["calendar"] = {"error": str(calendar_result)}

        # Generate human-readable brief
        unread = summary.get("email", {}).get("unread_count", 0)
        event_count = summary.get("calendar", {}).get("event_count", 0)

        summary["brief"] = (
            f"Good morning! You have {unread} unread emails and "
            f"{event_count} events scheduled for today."
        )

        if event_count > 0:
            first_event = summary["calendar"]["events"][0]
            summary["brief"] += (
                f" Your first meeting is '{first_event['title']}' "
                f"starting at {first_event['start']}."
            )

        return summary

    async def _handle_natural_query(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle a natural language workspace query.

        This is the main entry point for intelligent routing.
        """
        query = payload.get("query", "")

        if not query:
            return {"error": "No query provided"}

        # Detect intent
        intent, confidence, metadata = self._intent_detector.detect(query)

        logger.info(
            f"Detected workspace intent: {intent.value} (confidence={confidence:.2f})"
        )

        # Route based on intent
        if intent == WorkspaceIntent.CHECK_EMAIL:
            return await self._fetch_unread_emails({
                "limit": payload.get("limit", 5),
            })

        elif intent == WorkspaceIntent.CHECK_CALENDAR:
            dates = metadata.get("extracted_dates", {})
            return await self._check_calendar({
                "date": dates.get("today") or dates.get("tomorrow"),
                "days": 1,
            })

        elif intent == WorkspaceIntent.DRAFT_EMAIL:
            names = metadata.get("extracted_names", [])
            # If we have a name, we'd need to look up the email
            return {
                "status": "draft_ready",
                "message": "Ready to draft email",
                "detected_recipient": names[0] if names else None,
                "instructions": "Please provide: to, subject, and body",
            }

        elif intent == WorkspaceIntent.SEND_EMAIL:
            return {
                "status": "send_ready",
                "message": "Ready to send email",
                "instructions": "Please provide: to, subject, and body",
            }

        elif intent == WorkspaceIntent.DAILY_BRIEFING:
            return await self._get_workspace_summary({})

        elif intent == WorkspaceIntent.GET_CONTACTS:
            names = metadata.get("extracted_names", [])
            return await self._get_contacts({
                "query": names[0] if names else None,
            })

        elif intent == WorkspaceIntent.CREATE_EVENT:
            return {
                "status": "event_ready",
                "message": "Ready to create calendar event",
                "instructions": "Please provide: title, start, and optionally end, description, location, attendees",
            }

        else:
            return {
                "status": "unknown_intent",
                "detected_intent": intent.value,
                "confidence": confidence,
                "message": "I'm not sure what workspace action you'd like. Try asking about emails, calendar, or contacts.",
            }

    async def _handle_workspace_message(self, message: AgentMessage) -> None:
        """Handle incoming workspace messages from other agents."""
        if message.payload.get("type") != "workspace_request":
            return

        query = message.payload.get("query", "")
        action = message.payload.get("action")

        try:
            if action:
                result = await self.execute_task({
                    "action": action,
                    **message.payload,
                })
            else:
                result = await self._handle_natural_query({"query": query})

            # Send response
            if self.message_bus:
                await self.message_bus.respond(
                    message,
                    payload={
                        "type": "workspace_response",
                        "result": result,
                    },
                    from_agent=self.agent_name,
                )
        except Exception as e:
            logger.exception(f"Error handling workspace message: {e}")
            if self.message_bus:
                await self.message_bus.respond(
                    message,
                    payload={
                        "type": "workspace_response",
                        "error": str(e),
                    },
                    from_agent=self.agent_name,
                )

    # =========================================================================
    # v3.0: Google Sheets Operations
    # =========================================================================

    async def _read_spreadsheet(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Read data from a Google Sheet.

        Args:
            payload: Dict with:
                - spreadsheet_id: Google Sheets ID (from URL)
                - sheet_name: Optional sheet name (default: first sheet)
                - range: A1 notation range (e.g., "A1:D10")
                - header_row: Whether first row is headers (default: True)

        Returns:
            Dict with data and metadata
        """
        start_time = asyncio.get_event_loop().time()
        spreadsheet_id = payload.get("spreadsheet_id", "")
        sheet_name = payload.get("sheet_name")
        cell_range = payload.get("range", "A1:Z100")
        header_row = payload.get("header_row", True)

        if not spreadsheet_id:
            return {"error": "spreadsheet_id is required", "success": False}

        result = {"success": False}

        # Try gspread first (Tier 1)
        if GOOGLE_SHEETS_AVAILABLE and gspread:
            try:
                loop = asyncio.get_event_loop()
                data = await loop.run_in_executor(
                    None,
                    lambda: self._read_sheet_sync(spreadsheet_id, sheet_name, cell_range, header_row),
                )
                result = {
                    "success": True,
                    "data": data["values"],
                    "headers": data.get("headers"),
                    "row_count": len(data["values"]),
                    "tier_used": "google_api",
                    "execution_time_ms": (asyncio.get_event_loop().time() - start_time) * 1000,
                }

                # Log experience
                await self._log_experience(
                    action="read_spreadsheet",
                    input_data={"spreadsheet_id": spreadsheet_id[:8] + "...", "range": cell_range},
                    output_data=result,
                    success=True,
                )

                return result

            except Exception as e:
                logger.warning(f"gspread read failed: {e}")

        # Fallback to Computer Use (Tier 3)
        if self._unified_executor and self._unified_executor._computer_use:
            try:
                await self._unified_executor._switch_to_app_with_spatial_awareness("Safari", narrate=True)

                goal = (
                    f"Navigate to Google Sheets (docs.google.com/spreadsheets/d/{spreadsheet_id}), "
                    f"and read the data from range {cell_range}. List the values you see."
                )
                cu_result = await self._unified_executor._computer_use.run(goal=goal)

                if cu_result and cu_result.success:
                    result = {
                        "success": True,
                        "raw_response": cu_result.final_message,
                        "tier_used": "computer_use",
                        "execution_time_ms": (asyncio.get_event_loop().time() - start_time) * 1000,
                    }
                    return result

            except Exception as e:
                logger.warning(f"Computer Use read failed: {e}")

        result["error"] = "All sheet reading methods failed"
        return result

    def _read_sheet_sync(
        self,
        spreadsheet_id: str,
        sheet_name: Optional[str],
        cell_range: str,
        header_row: bool,
    ) -> Dict[str, Any]:
        """Synchronous sheet reading via gspread."""
        # Use OAuth2 credentials from the workspace client
        if self._client and self._client._creds:
            gc = gspread.authorize(self._client._creds)
        else:
            # Try service account
            creds = ServiceAccountCredentials.from_service_account_file(
                os.getenv("GOOGLE_SERVICE_ACCOUNT_PATH", ""),
                scopes=[
                    "https://www.googleapis.com/auth/spreadsheets.readonly",
                    "https://www.googleapis.com/auth/drive.readonly",
                ],
            )
            gc = gspread.authorize(creds)

        spreadsheet = gc.open_by_key(spreadsheet_id)

        if sheet_name:
            worksheet = spreadsheet.worksheet(sheet_name)
        else:
            worksheet = spreadsheet.sheet1

        values = worksheet.get(cell_range)

        result = {"values": values}

        if header_row and values:
            result["headers"] = values[0]
            result["values"] = values[1:]

        return result

    async def _write_spreadsheet(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Write data to a Google Sheet.

        Args:
            payload: Dict with:
                - spreadsheet_id: Google Sheets ID
                - sheet_name: Optional sheet name
                - range: A1 notation start cell (e.g., "A1")
                - values: 2D list of values to write
                - mode: "update" (overwrite) or "append"

        Returns:
            Dict with status and metadata
        """
        start_time = asyncio.get_event_loop().time()
        spreadsheet_id = payload.get("spreadsheet_id", "")
        sheet_name = payload.get("sheet_name")
        cell_range = payload.get("range", "A1")
        values = payload.get("values", [])
        mode = payload.get("mode", "update")

        if not spreadsheet_id:
            return {"error": "spreadsheet_id is required", "success": False}

        if not values:
            return {"error": "values list is required", "success": False}

        result = {"success": False}

        # Try gspread first
        if GOOGLE_SHEETS_AVAILABLE and gspread:
            try:
                loop = asyncio.get_event_loop()
                write_result = await loop.run_in_executor(
                    None,
                    lambda: self._write_sheet_sync(spreadsheet_id, sheet_name, cell_range, values, mode),
                )
                result = {
                    "success": True,
                    "cells_updated": write_result.get("cells_updated", 0),
                    "tier_used": "google_api",
                    "execution_time_ms": (asyncio.get_event_loop().time() - start_time) * 1000,
                }

                # Log experience
                await self._log_experience(
                    action="write_spreadsheet",
                    input_data={
                        "spreadsheet_id": spreadsheet_id[:8] + "...",
                        "range": cell_range,
                        "row_count": len(values),
                    },
                    output_data=result,
                    success=True,
                )

                return result

            except Exception as e:
                logger.warning(f"gspread write failed: {e}")

        # Fallback to Computer Use
        if self._unified_executor and self._unified_executor._computer_use:
            try:
                await self._unified_executor._switch_to_app_with_spatial_awareness("Safari", narrate=True)

                # Flatten values for Computer Use instruction
                values_str = str(values[:5])  # Limit for prompt size

                goal = (
                    f"Navigate to Google Sheets (docs.google.com/spreadsheets/d/{spreadsheet_id}), "
                    f"go to cell {cell_range}, and enter these values: {values_str}"
                )
                cu_result = await self._unified_executor._computer_use.run(goal=goal)

                if cu_result and cu_result.success:
                    result = {
                        "success": True,
                        "raw_response": cu_result.final_message,
                        "tier_used": "computer_use",
                        "execution_time_ms": (asyncio.get_event_loop().time() - start_time) * 1000,
                    }
                    return result

            except Exception as e:
                logger.warning(f"Computer Use write failed: {e}")

        result["error"] = "All sheet writing methods failed"
        return result

    def _write_sheet_sync(
        self,
        spreadsheet_id: str,
        sheet_name: Optional[str],
        cell_range: str,
        values: List[List[Any]],
        mode: str,
    ) -> Dict[str, Any]:
        """Synchronous sheet writing via gspread."""
        if self._client and self._client._creds:
            gc = gspread.authorize(self._client._creds)
        else:
            creds = ServiceAccountCredentials.from_service_account_file(
                os.getenv("GOOGLE_SERVICE_ACCOUNT_PATH", ""),
                scopes=[
                    "https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive",
                ],
            )
            gc = gspread.authorize(creds)

        spreadsheet = gc.open_by_key(spreadsheet_id)

        if sheet_name:
            worksheet = spreadsheet.worksheet(sheet_name)
        else:
            worksheet = spreadsheet.sheet1

        if mode == "append":
            worksheet.append_rows(values)
            cells_updated = len(values) * (len(values[0]) if values else 0)
        else:
            worksheet.update(cell_range, values)
            cells_updated = len(values) * (len(values[0]) if values else 0)

        return {"cells_updated": cells_updated}

    # =========================================================================
    # Convenience methods for direct access
    # =========================================================================

    async def check_schedule(self, date_str: str = "today") -> Dict[str, Any]:
        """Quick method to check today's schedule."""
        return await self.execute_task({
            "action": "check_calendar_events",
            "date": date_str,
            "days": 1,
        })

    async def check_emails(self, limit: int = 5) -> Dict[str, Any]:
        """Quick method to check unread emails."""
        return await self.execute_task({
            "action": "fetch_unread_emails",
            "limit": limit,
        })

    async def draft_reply(
        self,
        to: str,
        subject: str,
        body: str,
    ) -> Dict[str, Any]:
        """Quick method to draft an email."""
        return await self.execute_task({
            "action": "draft_email_reply",
            "to": to,
            "subject": subject,
            "body": body,
        })

    async def briefing(self) -> Dict[str, Any]:
        """Get daily briefing."""
        return await self.execute_task({
            "action": "workspace_summary",
        })

    def is_workspace_query(self, query: str) -> Tuple[bool, float]:
        """
        Check if a query should be routed to this agent.

        Used by the orchestrator for intelligent routing.
        """
        return self._intent_detector.is_workspace_query(query)

    def get_stats(self) -> Dict[str, Any]:
        """Get agent statistics including unified executor metrics."""
        stats = {
            "email_queries": self._email_queries,
            "calendar_queries": self._calendar_queries,
            "emails_sent": self._emails_sent,
            "drafts_created": self._drafts_created,
            "events_created": self._events_created,
            "documents_created": self._documents_created,
            "fallback_uses": self._fallback_uses,
            "capabilities": list(self.capabilities),
            "version": "3.0.0",
            "trinity_integration": {
                "experience_forwarder_available": EXPERIENCE_FORWARDER_AVAILABLE,
                "model_serving_available": UNIFIED_MODEL_SERVING_AVAILABLE,
                "sheets_available": GOOGLE_SHEETS_AVAILABLE,
            },
        }

        # Add unified executor stats if available
        if self._unified_executor:
            stats["unified_executor"] = self._unified_executor.get_stats()

        return stats


# ---------------------------------------------------------------------------
# v237.0: Singleton getter (required by tiered_command_router.py:1072)
# ---------------------------------------------------------------------------
_workspace_agent_instance: Optional["GoogleWorkspaceAgent"] = None


async def get_google_workspace_agent() -> Optional["GoogleWorkspaceAgent"]:
    """Get the GoogleWorkspaceAgent from Neural Mesh registry, or create standalone.

    Tier 1: Check running Neural Mesh coordinator for a registered instance.
    Tier 2: Create a standalone instance (no coordinator required).

    Does NOT create a coordinator as a side effect.
    """
    global _workspace_agent_instance
    if _workspace_agent_instance is not None:
        # Staleness check — don't return a stopped/dead agent
        if hasattr(_workspace_agent_instance, '_running') and not _workspace_agent_instance._running:
            _workspace_agent_instance = None
        else:
            return _workspace_agent_instance

    # Tier 1: Try the running Neural Mesh (without triggering creation)
    try:
        from neural_mesh.neural_mesh_coordinator import _coordinator
        if _coordinator is not None and _coordinator._running:
            for agent in _coordinator.get_all_agents():
                if isinstance(agent, GoogleWorkspaceAgent):
                    _workspace_agent_instance = agent
                    return _workspace_agent_instance
    except Exception:
        pass

    # Tier 2: Create standalone instance
    try:
        instance = GoogleWorkspaceAgent()
        await instance.on_initialize()
        _workspace_agent_instance = instance
        return _workspace_agent_instance
    except Exception as exc:
        logger.error("Failed to create standalone GoogleWorkspaceAgent: %s", exc)
        return None
