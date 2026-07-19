"""Quarantined supervisor zones — Migration Slice 2 (2026-07-19)."""
from __future__ import annotations
import asyncio, base64, collections, dataclasses, datetime, enum, functools, hashlib, hmac, json, logging, os, re, secrets, sqlite3, subprocess, sys, threading, time, uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime as _dt, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import (Any, Awaitable, Callable, Dict, List, NamedTuple,
                    Optional, Sequence, Set, Tuple, Union)
logger = logging.getLogger("quarantine.slice2")


class DataFlywheelManager:
    """
    Self-improving learning loop that continuously improves JARVIS.

    The Data Flywheel captures user interactions, extracts learning signals,
    and feeds them back into the training pipeline for continuous improvement.

    Flow:
    1. Capture: Log all user interactions with context
    2. Process: Extract learning signals (positive/negative feedback)
    3. Queue: Buffer experiences for batch training
    4. Train: Trigger training jobs via Reactor Core
    5. Deploy: Hot-swap improved models
    6. Evaluate: A/B test improvements
    """

    def __init__(
        self,
        experience_dir: Optional[Path] = None,
        batch_size: int = 100,
        flush_interval: float = 300.0,  # 5 minutes
        min_quality_score: float = 0.7,
    ) -> None:
        self._experience_dir = experience_dir or Path.home() / ".jarvis" / "experiences"
        self._experience_dir.mkdir(parents=True, exist_ok=True)

        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._min_quality_score = min_quality_score

        # Experience buffer
        self._experience_buffer: List[Dict[str, Any]] = []
        self._buffer_lock = asyncio.Lock()

        # Statistics
        self._stats = {
            "total_captured": 0,
            "total_processed": 0,
            "total_queued": 0,
            "batches_flushed": 0,
            "training_jobs_triggered": 0,
            "quality_rejections": 0,
            "last_flush_time": None,
            "last_training_trigger": None,
        }

        # Background tasks
        self._flush_task: Optional[asyncio.Task] = None
        self._running = False

        # Training pipeline connection
        self._reactor_core_url = os.getenv("REACTOR_CORE_URL", "http://localhost:8090")
        self._training_enabled = os.getenv("FLYWHEEL_TRAINING_ENABLED", "true").lower() == "true"

    async def start(self) -> bool:
        """Start the data flywheel background processing."""
        if self._running:
            return True

        self._running = True
        self._flush_task = create_safe_task(self._flush_loop())
        return True

    async def stop(self) -> None:
        """Stop the data flywheel and flush remaining experiences."""
        self._running = False

        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass

        # Final flush
        await self._flush_buffer()

    async def capture_experience(
        self,
        interaction_type: str,
        user_input: str,
        system_response: str,
        context: Optional[Dict[str, Any]] = None,
        feedback: Optional[str] = None,  # positive, negative, neutral
        quality_score: Optional[float] = None,
    ) -> str:
        """
        Capture a user interaction for the learning flywheel.

        Returns:
            Experience ID for tracking
        """
        experience_id = f"exp_{int(time.time() * 1000)}_{os.urandom(4).hex()}"

        experience = {
            "id": experience_id,
            "timestamp": datetime.now().isoformat(),
            "type": interaction_type,
            "user_input": user_input,
            "system_response": system_response,
            "context": context or {},
            "feedback": feedback,
            "quality_score": quality_score,
            "metadata": {
                "source": "unified_kernel",
                "version": KERNEL_VERSION,
            },
        }

        async with self._buffer_lock:
            self._experience_buffer.append(experience)
            self._stats["total_captured"] += 1

        # Check if we should trigger immediate flush
        if len(self._experience_buffer) >= self._batch_size:
            # v210.0: Use safe task to prevent "Future exception was never retrieved"
            create_safe_task(self._flush_buffer(), name="experience_flush")

        return experience_id

    async def _flush_loop(self) -> None:
        """Background loop to periodically flush experiences."""
        while self._running:
            try:
                await asyncio.sleep(self._flush_interval)
                await self._flush_buffer()
            except asyncio.CancelledError:
                break
            except Exception as e:
                # Log but don't crash the flywheel
                pass

    async def _flush_buffer(self) -> None:
        """Flush buffered experiences to disk and potentially trigger training."""
        async with self._buffer_lock:
            if not self._experience_buffer:
                return

            experiences_to_flush = self._experience_buffer.copy()
            self._experience_buffer.clear()

        # Filter by quality
        quality_experiences = []
        for exp in experiences_to_flush:
            score = exp.get("quality_score")
            if score is None or score >= self._min_quality_score:
                quality_experiences.append(exp)
                self._stats["total_processed"] += 1
            else:
                self._stats["quality_rejections"] += 1

        if not quality_experiences:
            return

        # Write to disk
        batch_file = self._experience_dir / f"batch_{int(time.time())}.jsonl"
        try:
            with open(batch_file, "w") as f:
                for exp in quality_experiences:
                    f.write(json.dumps(exp) + "\n")

            self._stats["batches_flushed"] += 1
            self._stats["total_queued"] += len(quality_experiences)
            self._stats["last_flush_time"] = datetime.now().isoformat()
        except Exception:
            pass

        # Trigger training if enabled and we have enough data
        if self._training_enabled and self._stats["total_queued"] >= self._batch_size * 10:
            await self._trigger_training()

    async def _trigger_training(self) -> bool:
        """Trigger a training job on Reactor Core via ReactorCoreClient."""
        # v2.1: Use ReactorCoreClient instead of raw HTTP
        try:
            from backend.clients.reactor_core_client import check_and_trigger_training, TrainingPriority
            job = await check_and_trigger_training(
                experience_count=self._stats.get("total_queued", 0),
                priority=TrainingPriority.NORMAL,
            )
            if job:
                logger.info(f"[DataFlywheel] Training triggered via ReactorCoreClient: {job.job_id}")
                self._stats["training_jobs_triggered"] += 1
                self._stats["last_training_trigger"] = datetime.now().isoformat()
                return True
        except Exception as e:
            logger.warning(f"[DataFlywheel] Training trigger failed: {e}")
        return False

    def get_stats(self) -> Dict[str, Any]:
        """Get flywheel statistics."""
        return {
            **self._stats,
            "buffer_size": len(self._experience_buffer),
            "running": self._running,
        }


class StartupSummaryTable:
    """
    Collects and displays a summary table of startup phases.

    Tracks phase name, status, duration, and any notes.
    """

    def __init__(self) -> None:
        self._phases: List[Dict[str, Any]] = []

    def add_phase(
        self,
        name: str,
        status: str,
        duration_ms: float,
        notes: str = "",
    ) -> None:
        """Add a phase result to the summary."""
        self._phases.append({
            "name": name,
            "status": status,
            "duration_ms": duration_ms,
            "notes": notes,
        })

    def print_table(self) -> None:
        """Print the formatted summary table."""
        if not self._phases:
            return

        # Calculate column widths
        name_width = max(len(p["name"]) for p in self._phases)
        name_width = max(name_width, 12)  # Minimum width

        # Header
        print()
        print("╔" + "═" * (name_width + 2) + "╦" + "═" * 10 + "╦" + "═" * 12 + "╦" + "═" * 30 + "╗")
        print(f"║ {'Phase':<{name_width}} ║ {'Status':^8} ║ {'Duration':^10} ║ {'Notes':<28} ║")
        print("╠" + "═" * (name_width + 2) + "╬" + "═" * 10 + "╬" + "═" * 12 + "╬" + "═" * 30 + "╣")

        # Rows
        for phase in self._phases:
            name = phase["name"][:name_width]
            status = phase["status"]
            duration = f"{phase['duration_ms']:.0f}ms"
            notes = phase["notes"][:28] if phase["notes"] else ""

            # Color status
            if status == "✓":
                status_display = "\033[32m✓ OK\033[0m    "
            elif status == "✗":
                status_display = "\033[31m✗ FAIL\033[0m  "
            elif status == "⚠":
                status_display = "\033[33m⚠ WARN\033[0m  "
            else:
                status_display = f"{status:^8}"

            print(f"║ {name:<{name_width}} ║ {status_display} ║ {duration:>10} ║ {notes:<28} ║")

        # Footer
        print("╚" + "═" * (name_width + 2) + "╩" + "═" * 10 + "╩" + "═" * 12 + "╩" + "═" * 30 + "╝")

        # Total duration
        total_ms = sum(p["duration_ms"] for p in self._phases)
        success_count = sum(1 for p in self._phases if p["status"] == "✓")
        total_count = len(self._phases)

        print(f"\n  Total: {total_ms:.0f}ms ({total_ms/1000:.2f}s) | Phases: {success_count}/{total_count} successful")
        print()


async def _direct_health_check(host: str, port: int, timeout: float = 5.0) -> Dict[str, Any]:
    """
    v201.1: Perform direct HTTP health check (used when kernel not running).

    Args:
        host: Hostname or IP address
        port: Port number
        timeout: Request timeout in seconds

    Returns:
        Dict with 'reachable', 'status', 'data' keys
    """
    result: Dict[str, Any] = {"reachable": False, "status": "unknown", "data": {}}

    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            url = f"http://{host}:{port}/health"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                result["reachable"] = True
                result["status"] = "healthy" if resp.status == 200 else f"http_{resp.status}"
                if resp.status == 200:
                    try:
                        result["data"] = await resp.json()
                    except Exception:
                        result["data"] = {"raw": await resp.text()}
    except ImportError:
        # Fallback to urllib if aiohttp not available - run in thread to avoid blocking
        import urllib.request
        import urllib.error

        def _sync_health_check() -> Dict[str, Any]:
            """Synchronous health check for thread execution."""
            sync_result: Dict[str, Any] = {"reachable": False, "status": "unknown", "data": {}}
            try:
                check_url = f"http://{host}:{port}/health"
                req = urllib.request.Request(check_url, method='GET')
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    sync_result["reachable"] = True
                    sync_result["status"] = "healthy" if resp.status == 200 else f"http_{resp.status}"
            except urllib.error.URLError:
                sync_result["status"] = "unreachable"
            except Exception:
                sync_result["status"] = "error"
            return sync_result

        try:
            # v206.0: Run blocking urllib in thread to avoid blocking event loop
            result = await asyncio.to_thread(_sync_health_check)
        except Exception:
            result["status"] = "error"
    except Exception as e:
        result["status"] = f"error: {type(e).__name__}"

    return result


def get_browser_stability_manager():
    """
    v210.0: Get the enterprise-grade BrowserStabilityManager.
    
    Returns the modular stability manager from backend.core.browser_stability
    which provides:
    - Proactive memory pressure monitoring
    - Chrome stability flags with Metal API bypass
    - Browser-specific circuit breaker
    - Intelligent recovery strategies
    - Integration with crash recovery coordinator
    
    Falls back to None if modular implementation is not available.
    """
    if MODULAR_BROWSER_STABILITY_AVAILABLE and _modular_get_stability_manager is not None:
        try:
            return _modular_get_stability_manager()
        except Exception as e:
            _logger = logging.getLogger("unified_supervisor.browser")
            _logger.warning(f"[v210.0] Failed to get stability manager: {e}")
    return None


def get_modular_shutdown_coordinator():
    """
    v210.0: Get the modular ShutdownCoordinator for graceful shutdown.
    
    The ShutdownCoordinator provides:
    - Registration of child processes for cleanup
    - Ordered shutdown callbacks
    - Timeout-based termination escalation
    - Integration with crash recovery
    
    Returns None if modular implementation is not available.
    """
    if MODULAR_KERNEL_AVAILABLE and ModularShutdownCoordinator is not None:
        try:
            return ModularShutdownCoordinator()
        except Exception:
            pass
    return None


def get_modular_signal_protector():
    """
    v210.0: Get the modular SignalProtector for signal-safe critical sections.
    
    The SignalProtector provides:
    - Context manager for protecting critical code from signal interruption
    - Decorator for signal-safe functions
    - Deferred signal delivery after protected sections
    
    Returns None if modular implementation is not available.
    """
    if MODULAR_KERNEL_AVAILABLE and ModularSignalProtector is not None:
        try:
            return ModularSignalProtector()
        except Exception:
            pass
    return None


async def _check_spawn_admission_async(
    component: str,
    min_gb: float = 1.5,
) -> Tuple[bool, str]:
    """Async pre-spawn admission check using quantizer-backed memory snapshot."""
    snapshot = await _read_startup_memory_snapshot_async(
        f"spawn:{component}",
        include_reservations=True,
        refresh_quantizer=True,
    )
    return _check_spawn_admission(
        component,
        min_gb=min_gb,
        available_gb=snapshot.get("available_gb"),
        memory_snapshot=snapshot,
    )


async def initialize_trinity_connector(
    websocket_manager: Any = None,
    voice_system: Any = None,
    menu_bar: Any = None,
    event_bus: Any = None,
) -> bool:
    """Initialize the Trinity connector (call from kernel startup)."""
    connector = get_trinity_connector()
    return await connector.initialize(
        websocket_manager=websocket_manager,
        voice_system=voice_system,
        menu_bar=menu_bar,
        event_bus=event_bus,
    )


class ConsentRecord(NamedTuple):
    """Individual consent record."""
    consent_id: str
    user_id: str
    purpose_id: str
    granted: bool
    timestamp: float
    method: str  # explicit, implicit, withdrawal
    version: str  # Consent policy version
    ip_address: Optional[str]
    user_agent: Optional[str]
    proof: Optional[str]  # Signature or token


class ConsentPurpose(NamedTuple):
    """Consent purpose definition."""
    purpose_id: str
    name: str
    description: str
    legal_basis: str  # consent, contract, legal_obligation, legitimate_interest
    data_categories: List[str]
    retention_days: int
    third_party_sharing: bool
    required: bool  # Required for service
    created_at: float


class DataSubjectRequest(NamedTuple):
    """GDPR data subject request."""
    request_id: str
    user_id: str
    request_type: str  # access, rectification, erasure, portability, restriction
    status: str  # pending, processing, completed, rejected
    created_at: float
    due_date: float
    completed_at: Optional[float]
    notes: str
    data_delivered: Optional[str]


class SigningKey(NamedTuple):
    """Signing key."""
    key_id: str
    key_type: str
    public_key: str
    private_key_ref: str  # Reference to secure storage
    algorithm: str
    created_at: float
    expires_at: Optional[float]
    owner: str
    status: str  # active, revoked, expired


class DigitalSignature(NamedTuple):
    """Digital signature record."""
    signature_id: str
    document_hash: str
    signer_id: str
    key_id: str
    algorithm: str
    signature_value: str
    timestamp: float
    certificate_chain: Optional[List[str]]
    metadata: Dict[str, Any]


class SignatureVerification(NamedTuple):
    """Signature verification result."""
    is_valid: bool
    signature_id: str
    signer_id: str
    timestamp: float
    algorithm: str
    reason: str


class AudioInitPhase:
    """Phases of AudioBus initialization for progress tracking."""
    IMPORT = "import"
    DEVICE_QUERY = "device_query"
    PROFILE_CHECK = "profile_check"
    STREAM_OPEN = "stream_open"
    STREAM_START = "stream_start"


class SignatureAlgorithm(NamedTuple):
    """Signature algorithm specification."""
    algorithm_id: str
    name: str
    hash_algorithm: str
    key_type: str
    key_size: int


def get_startup_display(enabled: bool = True) -> StartupProgressDisplay:
    """Get or create the global startup display instance."""
    global _startup_display
    if _startup_display is None:
        _startup_display = StartupProgressDisplay(enabled=enabled)
    return _startup_display


def get_process_cleaner() -> ParallelProcessCleaner:
    """Get the global process cleaner."""
    global _process_cleaner
    if _process_cleaner is None:
        _process_cleaner = ParallelProcessCleaner()
    return _process_cleaner


class SessionStore(NamedTuple):
    """Session store configuration."""
    store_id: str
    store_type: str  # memory, redis, database
    config: Dict[str, Any]
    default_ttl: float


async def shutdown_trinity_connector() -> None:
    """Shutdown the Trinity connector."""
    global _trinity_connector
    if _trinity_connector:
        await _trinity_connector.shutdown()
        _trinity_connector = None


class IPCRequest:
    """IPC request from a client."""
    command: IPCCommand
    args: Dict[str, Any] = field(default_factory=dict)


def _get_env_write_log() -> List[Dict[str, str]]:
    """Return the env var write log for diagnostics (e.g. health endpoints)."""
    return list(_ENV_WRITE_LOG)
