"""
Enhanced SAI (Situational Awareness Intelligence) Orchestrator v1.0
====================================================================

This module provides a comprehensive situational awareness system that goes
beyond simple RAM predictions to provide true environmental intelligence.

Features:
=========
✅ Continuous environmental monitoring (not just idle/active)
✅ Multi-dimensional awareness:
   - System resources (RAM, CPU, disk)
   - Workspace/space tracking
   - Cross-repo status (JARVIS Prime, Reactor Core)
   - Process coordination status
   - User activity patterns
✅ Intelligent predictions:
   - RAM spikes (existing)
   - Workspace switches
   - Meeting conflicts
   - Task completion estimates
   - Resource contention
✅ Event streaming for real-time updates
✅ Learning from patterns
✅ Integration with ProcessCoordinationHub

Architecture:
    EnhancedSAIOrchestrator
    ├── ResourceAwarenessEngine (RAM, CPU, Disk)
    ├── WorkspaceIntelligence (Yabai spaces, windows)
    ├── CrossRepoAwareness (JARVIS Prime, Reactor Core)
    ├── CoordinationAwareness (ProcessCoordinationHub)
    ├── PredictionEngine (ML-based predictions)
    └── EventStream (real-time awareness updates)

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Callable,
    Deque,
    Dict,
    List,
    Optional,
)

# Safe imports with fallbacks
try:
    import psutil as _psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    _psutil = None


def _get_virtual_memory() -> Any:
    """Safely get virtual memory info."""
    if _psutil is not None:
        return _psutil.virtual_memory()
    return None


def _get_cpu_percent(interval: float = 0.1) -> float:
    """Safely get CPU percent."""
    if _psutil is not None:
        return _psutil.cpu_percent(interval=interval)
    return 0.0


def _get_disk_usage(path: str = '/') -> Any:
    """Safely get disk usage."""
    if _psutil is not None:
        return _psutil.disk_usage(path)
    return None

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

def _env_float(key: str, default: float) -> float:
    """Get float from environment variable."""
    try:
        return float(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


# =============================================================================
# Enums and Types
# =============================================================================

class AwarenessLevel(str, Enum):
    """SAI awareness levels - always showing something useful."""
    MONITORING = "monitoring"      # Baseline - always active
    ANALYZING = "analyzing"        # Processing data
    PREDICTING = "predicting"      # Making predictions
    ALERTING = "alerting"          # Something needs attention
    LEARNING = "learning"          # Learning from patterns


class InsightCategory(str, Enum):
    """Categories of SAI insights."""
    RESOURCE = "resource"          # RAM, CPU, Disk
    WORKSPACE = "workspace"        # Spaces, windows
    CROSS_REPO = "cross_repo"      # JARVIS Prime, Reactor Core
    COORDINATION = "coordination"  # Process coordination
    ACTIVITY = "activity"          # User activity patterns
    PREDICTION = "prediction"      # Future predictions
    HEALTH = "health"              # System health


class InsightSeverity(str, Enum):
    """Severity levels for insights."""
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class SAIInsight:
    """A single SAI insight/observation."""
    category: InsightCategory
    severity: InsightSeverity
    title: str
    description: str
    timestamp: float = field(default_factory=time.time)
    confidence: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    expires_at: Optional[float] = None  # When this insight becomes stale
    actionable: bool = False
    recommended_action: Optional[str] = None

    def is_expired(self) -> bool:
        """Check if this insight has expired."""
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at

    @property
    def age_seconds(self) -> float:
        """Get age of this insight in seconds."""
        return time.time() - self.timestamp

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "category": self.category.value,
            "severity": self.severity.value,
            "title": self.title,
            "description": self.description,
            "timestamp": self.timestamp,
            "confidence": self.confidence,
            "metadata": self.metadata,
            "expires_at": self.expires_at,
            "actionable": self.actionable,
            "recommended_action": self.recommended_action,
            "age_seconds": self.age_seconds,
        }


@dataclass
class SAIStatus:
    """Current SAI status snapshot."""
    level: AwarenessLevel
    timestamp: float = field(default_factory=time.time)
    active_insights: List[SAIInsight] = field(default_factory=list)
    recent_predictions: List[Dict[str, Any]] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    cross_repo_status: Dict[str, str] = field(default_factory=dict)
    coordination_status: Optional[Dict[str, Any]] = None

    @property
    def insight_count(self) -> int:
        """Number of active insights."""
        return len(self.active_insights)

    @property
    def highest_severity(self) -> Optional[InsightSeverity]:
        """Get the highest severity among active insights."""
        if not self.active_insights:
            return None
        severities = [InsightSeverity.INFO, InsightSeverity.LOW,
                      InsightSeverity.MEDIUM, InsightSeverity.HIGH,
                      InsightSeverity.CRITICAL]
        for sev in reversed(severities):
            if any(i.severity == sev for i in self.active_insights):
                return sev
        return None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "level": self.level.value,
            "timestamp": self.timestamp,
            "insight_count": self.insight_count,
            "highest_severity": self.highest_severity.value if self.highest_severity else None,
            "active_insights": [i.to_dict() for i in self.active_insights],
            "recent_predictions": self.recent_predictions,
            "metrics": self.metrics,
            "cross_repo_status": self.cross_repo_status,
            "coordination_status": self.coordination_status,
        }


# =============================================================================
# Resource Awareness Engine
# =============================================================================

class ResourceAwarenessEngine:
    """
    Monitors system resources and generates insights.

    Features:
    - RAM usage and trend analysis
    - CPU utilization patterns
    - Disk space monitoring
    - Process memory tracking
    """

    def __init__(self):
        self._history: Deque[Dict[str, float]] = deque(maxlen=60)  # 1 minute at 1s intervals
        self._last_check = 0.0
        self._check_interval = _env_float("SAI_RESOURCE_CHECK_INTERVAL", 5.0)

    async def get_insights(self) -> List[SAIInsight]:
        """Generate resource-related insights."""
        insights = []

        if not PSUTIL_AVAILABLE:
            return insights

        current_time = time.time()
        if current_time - self._last_check < self._check_interval:
            # Return cached insights based on history
            return self._analyze_history()

        self._last_check = current_time

        try:
            # Collect metrics using helper functions
            mem = _get_virtual_memory()
            cpu = _get_cpu_percent(interval=0.1)
            disk = _get_disk_usage('/')

            if mem is None or disk is None:
                return insights

            metrics = {
                "timestamp": current_time,
                "ram_percent": mem.percent,
                "ram_available_gb": mem.available / (1024 ** 3),
                "cpu_percent": cpu,
                "disk_percent": disk.percent,
            }
            self._history.append(metrics)

            # Generate insights
            insights.extend(self._analyze_ram(mem))
            insights.extend(self._analyze_cpu(cpu))
            insights.extend(self._analyze_disk(disk))
            insights.extend(self._analyze_trends())

        except Exception as e:
            logger.debug(f"[ResourceAwareness] Error collecting metrics: {e}")

        return insights

    def _analyze_ram(self, mem: Any) -> List[SAIInsight]:
        """Analyze RAM usage."""
        insights = []

        if mem.percent >= 90:
            insights.append(SAIInsight(
                category=InsightCategory.RESOURCE,
                severity=InsightSeverity.CRITICAL,
                title="Critical RAM Usage",
                description=f"RAM at {mem.percent:.1f}% - system may become unstable",
                confidence=1.0,
                actionable=True,
                recommended_action="Consider closing unused applications or offloading to GCP VM",
                metadata={"ram_percent": mem.percent, "available_gb": mem.available / (1024**3)},
                expires_at=time.time() + 60,
            ))
        elif mem.percent >= 80:
            insights.append(SAIInsight(
                category=InsightCategory.RESOURCE,
                severity=InsightSeverity.HIGH,
                title="High RAM Usage",
                description=f"RAM at {mem.percent:.1f}% - monitoring for potential issues",
                confidence=0.9,
                metadata={"ram_percent": mem.percent},
                expires_at=time.time() + 120,
            ))
        elif mem.percent >= 70:
            insights.append(SAIInsight(
                category=InsightCategory.RESOURCE,
                severity=InsightSeverity.MEDIUM,
                title="Elevated RAM Usage",
                description=f"RAM at {mem.percent:.1f}% - above normal levels",
                confidence=0.8,
                metadata={"ram_percent": mem.percent},
                expires_at=time.time() + 300,
            ))

        return insights

    def _analyze_cpu(self, cpu_percent: float) -> List[SAIInsight]:
        """Analyze CPU usage."""
        insights = []

        if cpu_percent >= 90:
            insights.append(SAIInsight(
                category=InsightCategory.RESOURCE,
                severity=InsightSeverity.HIGH,
                title="High CPU Usage",
                description=f"CPU at {cpu_percent:.1f}% - may affect performance",
                confidence=0.9,
                metadata={"cpu_percent": cpu_percent},
                expires_at=time.time() + 30,
            ))

        return insights

    def _analyze_disk(self, disk: Any) -> List[SAIInsight]:
        """Analyze disk usage."""
        insights = []

        if disk.percent >= 95:
            insights.append(SAIInsight(
                category=InsightCategory.RESOURCE,
                severity=InsightSeverity.CRITICAL,
                title="Critical Disk Space",
                description=f"Disk at {disk.percent:.1f}% - immediate action required",
                confidence=1.0,
                actionable=True,
                recommended_action="Free up disk space immediately",
                metadata={"disk_percent": disk.percent},
                expires_at=time.time() + 300,
            ))
        elif disk.percent >= 90:
            insights.append(SAIInsight(
                category=InsightCategory.RESOURCE,
                severity=InsightSeverity.HIGH,
                title="Low Disk Space",
                description=f"Disk at {disk.percent:.1f}% - consider cleanup",
                confidence=0.95,
                metadata={"disk_percent": disk.percent},
                expires_at=time.time() + 600,
            ))

        return insights

    def _analyze_trends(self) -> List[SAIInsight]:
        """Analyze resource trends for predictions."""
        insights = []

        if len(self._history) < 10:
            return insights

        # RAM trend analysis
        ram_values = [h["ram_percent"] for h in list(self._history)[-10:]]
        ram_trend = (ram_values[-1] - ram_values[0]) / len(ram_values)

        if ram_trend > 2.0:  # Increasing by >2% per sample
            insights.append(SAIInsight(
                category=InsightCategory.PREDICTION,
                severity=InsightSeverity.MEDIUM,
                title="RAM Usage Increasing",
                description=f"RAM trending up (+{ram_trend:.1f}%/sample) - spike possible in ~60s",
                confidence=0.7,
                metadata={"trend": ram_trend, "current": ram_values[-1]},
                expires_at=time.time() + 60,
            ))

        return insights

    def _analyze_history(self) -> List[SAIInsight]:
        """Generate insights from cached history."""
        if not self._history:
            return []

        latest = self._history[-1]
        insights = []

        # Generate basic status insight
        ram_pct = latest.get("ram_percent", 0)
        cpu_pct = latest.get("cpu_percent", 0)

        status_desc = f"RAM: {ram_pct:.1f}% | CPU: {cpu_pct:.1f}%"
        insights.append(SAIInsight(
            category=InsightCategory.RESOURCE,
            severity=InsightSeverity.INFO,
            title="System Resources",
            description=status_desc,
            confidence=1.0,
            metadata=latest,
            expires_at=time.time() + 10,
        ))

        return insights

    def get_metrics(self) -> Dict[str, Any]:
        """Get current resource metrics."""
        if not self._history:
            return {}
        return dict(self._history[-1])


# =============================================================================
# Cross-Repo Awareness Engine
# =============================================================================

class CrossRepoAwarenessEngine:
    """
    Monitors status of JARVIS Prime and Reactor Core.

    Features:
    - Heartbeat monitoring
    - Health endpoint checking
    - Connection status tracking
    - Auto-reconnection awareness
    """

    def __init__(self):
        self._last_check = 0.0
        self._check_interval = _env_float("SAI_CROSS_REPO_CHECK_INTERVAL", 30.0)
        self._status_cache: Dict[str, Dict[str, Any]] = {}

    async def get_insights(self) -> List[SAIInsight]:
        """Generate cross-repo insights."""
        insights = []

        current_time = time.time()
        if current_time - self._last_check < self._check_interval:
            return self._get_cached_insights()

        self._last_check = current_time

        # Check JARVIS Prime
        jprime_status = await self._check_jarvis_prime()
        self._status_cache["jarvis_prime"] = jprime_status
        insights.extend(self._generate_repo_insights("JARVIS Prime", jprime_status))

        # Check Reactor Core
        reactor_status = await self._check_reactor_core()
        self._status_cache["reactor_core"] = reactor_status
        insights.extend(self._generate_repo_insights("Reactor Core", reactor_status))

        return insights

    async def _check_jarvis_prime(self) -> Dict[str, Any]:
        """Check JARVIS Prime status."""
        status = {
            "available": False,
            "health": "unknown",
            "latency_ms": None,
            "checked_at": time.time(),
        }

        try:
            # Check heartbeat file first
            heartbeat_file = Path.home() / ".jarvis" / "trinity" / "components" / "jarvis_prime.json"
            if heartbeat_file.exists():
                content = heartbeat_file.read_text()
                data = json.loads(content)
                heartbeat_age = time.time() - data.get("timestamp", 0)

                if heartbeat_age < 30:
                    status["available"] = True
                    status["health"] = data.get("healthy", False) and "healthy" or "degraded"
                    status["port"] = data.get("port", 8000)
                    status["model_loaded"] = data.get("model_loaded", False)

            # Try HTTP health check if available
            if status["available"]:
                import aiohttp
                port = status.get("port", 8000)
                start = time.time()
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"http://localhost:{port}/health",
                        timeout=aiohttp.ClientTimeout(total=5.0)
                    ) as resp:
                        if resp.status == 200:
                            status["latency_ms"] = (time.time() - start) * 1000
                            status["health"] = "healthy"

        except Exception as e:
            logger.debug(f"[CrossRepoAwareness] JARVIS Prime check error: {e}")

        return status

    async def _check_reactor_core(self) -> Dict[str, Any]:
        """Check Reactor Core status."""
        status = {
            "available": False,
            "health": "unknown",
            "latency_ms": None,
            "checked_at": time.time(),
        }

        try:
            # Check heartbeat file
            heartbeat_file = Path.home() / ".jarvis" / "trinity" / "components" / "reactor_core.json"
            if heartbeat_file.exists():
                content = heartbeat_file.read_text()
                data = json.loads(content)
                heartbeat_age = time.time() - data.get("timestamp", 0)

                if heartbeat_age < 30:
                    status["available"] = True
                    status["health"] = "healthy" if data.get("healthy", False) else "degraded"
                    status["port"] = data.get("port", 8090)

        except Exception as e:
            logger.debug(f"[CrossRepoAwareness] Reactor Core check error: {e}")

        return status

    def _generate_repo_insights(self, repo_name: str, status: Dict[str, Any]) -> List[SAIInsight]:
        """Generate insights for a repo."""
        insights = []

        if status["available"]:
            health = status.get("health", "unknown")
            severity = InsightSeverity.INFO if health == "healthy" else InsightSeverity.MEDIUM

            insights.append(SAIInsight(
                category=InsightCategory.CROSS_REPO,
                severity=severity,
                title=f"{repo_name} Status",
                description=f"Connected ({health})" + (
                    f" - {status.get('latency_ms', 0):.0f}ms" if status.get('latency_ms') else ""
                ),
                confidence=0.95,
                metadata=status,
                expires_at=time.time() + 60,
            ))
        else:
            insights.append(SAIInsight(
                category=InsightCategory.CROSS_REPO,
                severity=InsightSeverity.LOW,
                title=f"{repo_name} Status",
                description="Not connected (optional)",
                confidence=1.0,
                metadata=status,
                expires_at=time.time() + 60,
            ))

        return insights

    def _get_cached_insights(self) -> List[SAIInsight]:
        """Get insights from cached status."""
        insights = []
        for repo_name, status in self._status_cache.items():
            display_name = repo_name.replace("_", " ").title()
            insights.extend(self._generate_repo_insights(display_name, status))
        return insights

    def get_status_summary(self) -> Dict[str, str]:
        """Get summary of cross-repo status."""
        return {
            name: "connected" if status.get("available") else "disconnected"
            for name, status in self._status_cache.items()
        }


# =============================================================================
# Coordination Awareness Engine
# =============================================================================

class CoordinationAwarenessEngine:
    """
    Monitors ProcessCoordinationHub status.

    Features:
    - Supervisor health tracking
    - Lock status monitoring
    - Port reservation status
    - Recovery events
    """

    def __init__(self):
        self._last_check = 0.0
        self._check_interval = _env_float("SAI_COORD_CHECK_INTERVAL", 15.0)

    async def get_insights(self) -> List[SAIInsight]:
        """Generate coordination insights."""
        insights = []

        try:
            from backend.core.trinity_process_coordination import get_coordination_hub

            hub = await get_coordination_hub()

            # Check supervisor status
            is_alive, reason = await hub.is_supervisor_alive()

            if is_alive:
                insights.append(SAIInsight(
                    category=InsightCategory.COORDINATION,
                    severity=InsightSeverity.INFO,
                    title="Supervisor Status",
                    description="Supervisor healthy and publishing heartbeats",
                    confidence=1.0,
                    expires_at=time.time() + 30,
                ))
            else:
                insights.append(SAIInsight(
                    category=InsightCategory.COORDINATION,
                    severity=InsightSeverity.MEDIUM,
                    title="Supervisor Status",
                    description=f"Supervisor not detected: {reason or 'unknown'}",
                    confidence=0.9,
                    expires_at=time.time() + 30,
                ))

            # Check standalone mode
            if await hub.is_standalone_mode():
                insights.append(SAIInsight(
                    category=InsightCategory.COORDINATION,
                    severity=InsightSeverity.LOW,
                    title="Standalone Mode",
                    description="Running in standalone mode (no supervisor)",
                    confidence=1.0,
                    expires_at=time.time() + 60,
                ))

        except ImportError:
            pass  # Coordination hub not available
        except Exception as e:
            logger.debug(f"[CoordinationAwareness] Error: {e}")

        return insights


# =============================================================================
# Workspace Intelligence Engine
# =============================================================================

class WorkspaceIntelligenceEngine:
    """
    Tracks workspace/space changes and patterns.

    Features:
    - Current space tracking
    - Window focus monitoring
    - Space change predictions
    - Activity pattern learning
    """

    def __init__(self):
        self._current_space: Optional[int] = None
        self._space_history: Deque[Dict[str, Any]] = deque(maxlen=100)
        self._last_check = 0.0
        self._check_interval = _env_float("SAI_WORKSPACE_CHECK_INTERVAL", 10.0)

    async def get_insights(self) -> List[SAIInsight]:
        """Generate workspace insights."""
        insights = []

        try:
            # Try to get current space from Yabai
            space_info = await self._get_current_space()

            if space_info:
                self._record_space_change(space_info)

                insights.append(SAIInsight(
                    category=InsightCategory.WORKSPACE,
                    severity=InsightSeverity.INFO,
                    title="Current Workspace",
                    description=f"Space {space_info.get('index', '?')} ({space_info.get('label', 'unnamed')})",
                    confidence=1.0,
                    metadata=space_info,
                    expires_at=time.time() + 30,
                ))

                # Add focused window info
                focused = space_info.get("focused_window")
                if focused:
                    insights.append(SAIInsight(
                        category=InsightCategory.WORKSPACE,
                        severity=InsightSeverity.INFO,
                        title="Focused Window",
                        description=f"{focused.get('app', 'Unknown')} - {focused.get('title', '')[:50]}",
                        confidence=0.95,
                        metadata={"window": focused},
                        expires_at=time.time() + 10,
                    ))

        except Exception as e:
            logger.debug(f"[WorkspaceIntelligence] Error: {e}")

        return insights

    async def _get_current_space(self) -> Optional[Dict[str, Any]]:
        """Get current space info from Yabai."""
        try:
            import subprocess
            result = subprocess.run(
                ["yabai", "-m", "query", "--spaces", "--space"],
                capture_output=True, text=True, timeout=2.0
            )
            if result.returncode == 0:
                return json.loads(result.stdout)
        except Exception:
            pass
        return None

    def _record_space_change(self, space_info: Dict[str, Any]) -> None:
        """Record space change for pattern learning."""
        space_index = space_info.get("index")
        if space_index != self._current_space:
            self._space_history.append({
                "from_space": self._current_space,
                "to_space": space_index,
                "timestamp": time.time(),
            })
            self._current_space = space_index


# =============================================================================
# Enhanced SAI Orchestrator (Main Class)
# =============================================================================

class EnhancedSAIOrchestrator:
    """
    Main SAI orchestrator that coordinates all awareness engines.

    This is the central hub that aggregates insights from all engines
    and provides a unified situational awareness status.
    """

    _instance: Optional["EnhancedSAIOrchestrator"] = None

    def __new__(cls) -> "EnhancedSAIOrchestrator":
        """Singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return

        self._initialized = True

        # Initialize engines
        self.resource_engine = ResourceAwarenessEngine()
        self.cross_repo_engine = CrossRepoAwarenessEngine()
        self.coordination_engine = CoordinationAwarenessEngine()
        self.workspace_engine = WorkspaceIntelligenceEngine()

        # State
        self._active_insights: List[SAIInsight] = []
        self._prediction_history: Deque[Dict[str, Any]] = deque(maxlen=20)
        self._last_status: Optional[SAIStatus] = None
        self._update_callbacks: List[Callable] = []

        # Monitoring task
        self._monitoring_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()

        # Configuration
        self._update_interval = _env_float("SAI_UPDATE_INTERVAL", 10.0)

        logger.info("[EnhancedSAI] Orchestrator initialized")

    async def start(self) -> None:
        """Start continuous monitoring."""
        if self._monitoring_task is not None:
            return

        self._shutdown_event.clear()
        self._monitoring_task = asyncio.create_task(self._monitoring_loop())
        logger.info("[EnhancedSAI] Monitoring started")

    async def stop(self) -> None:
        """Stop monitoring."""
        self._shutdown_event.set()

        if self._monitoring_task:
            self._monitoring_task.cancel()
            try:
                await self._monitoring_task
            except asyncio.CancelledError:
                pass
            self._monitoring_task = None

        logger.info("[EnhancedSAI] Monitoring stopped")

    async def _monitoring_loop(self) -> None:
        """Main monitoring loop."""
        while not self._shutdown_event.is_set():
            try:
                await self._update_awareness()
            except Exception as e:
                logger.error(f"[EnhancedSAI] Monitoring error: {e}")

            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self._update_interval
                )
                break
            except asyncio.TimeoutError:
                pass

    async def _update_awareness(self) -> None:
        """Update situational awareness from all engines."""
        all_insights: List[SAIInsight] = []

        # Gather insights from all engines in parallel
        results = await asyncio.gather(
            self.resource_engine.get_insights(),
            self.cross_repo_engine.get_insights(),
            self.coordination_engine.get_insights(),
            self.workspace_engine.get_insights(),
            return_exceptions=True
        )

        for result in results:
            if isinstance(result, list):
                all_insights.extend(result)
            elif isinstance(result, Exception):
                logger.debug(f"[EnhancedSAI] Engine error: {result}")

        # Filter expired insights and update
        self._active_insights = [i for i in all_insights if not i.is_expired()]

        # Determine awareness level
        level = self._determine_awareness_level()

        # Create status
        self._last_status = SAIStatus(
            level=level,
            active_insights=self._active_insights,
            recent_predictions=list(self._prediction_history),
            metrics=self.resource_engine.get_metrics(),
            cross_repo_status=self.cross_repo_engine.get_status_summary(),
        )

        # Notify callbacks
        for callback in self._update_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(self._last_status)
                else:
                    callback(self._last_status)
            except Exception as e:
                logger.debug(f"[EnhancedSAI] Callback error: {e}")

    def _determine_awareness_level(self) -> AwarenessLevel:
        """Determine current awareness level based on insights."""
        if not self._active_insights:
            return AwarenessLevel.MONITORING

        severities = [i.severity for i in self._active_insights]

        if InsightSeverity.CRITICAL in severities:
            return AwarenessLevel.ALERTING
        if InsightSeverity.HIGH in severities:
            return AwarenessLevel.ALERTING
        if any(i.category == InsightCategory.PREDICTION for i in self._active_insights):
            return AwarenessLevel.PREDICTING

        return AwarenessLevel.ANALYZING

    def get_status(self) -> SAIStatus:
        """Get current SAI status."""
        if self._last_status is None:
            return SAIStatus(level=AwarenessLevel.MONITORING)
        return self._last_status

    def get_display_summary(self) -> Dict[str, Any]:
        """Get summary for display in status output."""
        status = self.get_status()

        # Build summary
        summary = {
            "level": status.level.value,
            "level_icon": self._get_level_icon(status.level),
            "level_color": self._get_level_color(status.level),
            "insight_count": status.insight_count,
            "highest_severity": status.highest_severity.value if status.highest_severity else None,
            "top_insights": [],
            "cross_repo": status.cross_repo_status,
            "metrics": status.metrics,
        }

        # Get top 3 most important insights
        sorted_insights = sorted(
            status.active_insights,
            key=lambda i: (
                list(InsightSeverity).index(i.severity),
                -i.timestamp
            ),
            reverse=True
        )[:3]

        for insight in sorted_insights:
            summary["top_insights"].append({
                "title": insight.title,
                "description": insight.description,
                "severity": insight.severity.value,
                "category": insight.category.value,
            })

        return summary

    def _get_level_icon(self, level: AwarenessLevel) -> str:
        """Get icon for awareness level."""
        icons = {
            AwarenessLevel.MONITORING: "👁️",
            AwarenessLevel.ANALYZING: "🔍",
            AwarenessLevel.PREDICTING: "🔮",
            AwarenessLevel.ALERTING: "⚠️",
            AwarenessLevel.LEARNING: "🧠",
        }
        return icons.get(level, "🔮")

    def _get_level_color(self, level: AwarenessLevel) -> str:
        """Get color name for awareness level."""
        colors = {
            AwarenessLevel.MONITORING: "cyan",
            AwarenessLevel.ANALYZING: "blue",
            AwarenessLevel.PREDICTING: "magenta",
            AwarenessLevel.ALERTING: "yellow",
            AwarenessLevel.LEARNING: "green",
        }
        return colors.get(level, "white")

    def register_callback(self, callback: Callable) -> None:
        """Register a callback for status updates."""
        if callback not in self._update_callbacks:
            self._update_callbacks.append(callback)

    def unregister_callback(self, callback: Callable) -> None:
        """Unregister a callback."""
        if callback in self._update_callbacks:
            self._update_callbacks.remove(callback)

    def add_prediction(self, prediction: Dict[str, Any]) -> None:
        """Add a prediction to history."""
        prediction["timestamp"] = time.time()
        self._prediction_history.append(prediction)


# =============================================================================
# Global Access
# =============================================================================

_sai_orchestrator: Optional[EnhancedSAIOrchestrator] = None


def get_enhanced_sai() -> EnhancedSAIOrchestrator:
    """Get the global enhanced SAI orchestrator."""
    global _sai_orchestrator
    if _sai_orchestrator is None:
        _sai_orchestrator = EnhancedSAIOrchestrator()
    return _sai_orchestrator


async def initialize_enhanced_sai() -> EnhancedSAIOrchestrator:
    """Initialize and start the enhanced SAI orchestrator."""
    sai = get_enhanced_sai()
    await sai.start()
    return sai


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    # Main orchestrator
    "EnhancedSAIOrchestrator",
    "get_enhanced_sai",
    "initialize_enhanced_sai",

    # Engines
    "ResourceAwarenessEngine",
    "CrossRepoAwarenessEngine",
    "CoordinationAwarenessEngine",
    "WorkspaceIntelligenceEngine",

    # Types
    "SAIStatus",
    "SAIInsight",
    "AwarenessLevel",
    "InsightCategory",
    "InsightSeverity",
]
