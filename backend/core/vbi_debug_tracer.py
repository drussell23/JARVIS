"""
VBI Debug Tracer - Comprehensive debugging and pre-warming system for Voice Biometric Intelligence

This module provides:
1. Detailed step-by-step tracing of the entire VBI pipeline
2. ECAPA pre-warming at startup (no cold starts during unlock)
3. Dynamic timeout management
4. Health monitoring and auto-recovery

Version: 1.0.0
"""

import asyncio
import logging
import time
import os
import json
from datetime import datetime
from typing import Dict, Any, Optional, List, Callable
from dataclasses import dataclass, field
from enum import Enum
from contextlib import asynccontextmanager
import traceback

logger = logging.getLogger(__name__)


class VBIStage(Enum):
    """Stages in the VBI pipeline"""
    AUDIO_RECEIVE = "audio_receive"
    AUDIO_DECODE = "audio_decode"
    AUDIO_PREPROCESS = "audio_preprocess"
    ECAPA_EXTRACT = "ecapa_extract"
    SPEAKER_VERIFY = "speaker_verify"
    DECISION = "decision"
    UNLOCK_EXECUTE = "unlock_execute"
    RESPONSE_BUILD = "response_build"
    WEBSOCKET_SEND = "websocket_send"


class VBIStatus(Enum):
    """Status of VBI operations"""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


@dataclass
class VBITraceStep:
    """A single step in the VBI trace"""
    stage: VBIStage
    status: VBIStatus
    start_time: float
    end_time: Optional[float] = None
    duration_ms: Optional[float] = None
    details: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def complete(self, status: VBIStatus = VBIStatus.SUCCESS, details: Dict = None, error: str = None):
        """Mark step as complete"""
        self.end_time = time.time()
        self.duration_ms = (self.end_time - self.start_time) * 1000
        self.status = status
        if details:
            self.details.update(details)
        if error:
            self.error = error


@dataclass
class VBITrace:
    """Complete trace of a VBI request"""
    trace_id: str
    command: str
    start_time: float
    steps: List[VBITraceStep] = field(default_factory=list)
    audio_size_bytes: int = 0
    sample_rate: int = 0
    mime_type: str = ""
    speaker_confidence: float = 0.0
    speaker_name: str = ""
    final_status: VBIStatus = VBIStatus.PENDING
    total_duration_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Convert trace to dictionary for logging"""
        return {
            "trace_id": self.trace_id,
            "command": self.command,
            "timestamp": datetime.fromtimestamp(self.start_time).isoformat(),
            "total_duration_ms": self.total_duration_ms,
            "final_status": self.final_status.value,
            "audio": {
                "size_bytes": self.audio_size_bytes,
                "sample_rate": self.sample_rate,
                "mime_type": self.mime_type
            },
            "speaker": {
                "confidence": self.speaker_confidence,
                "name": self.speaker_name
            },
            "steps": [
                {
                    "stage": s.stage.value,
                    "status": s.status.value,
                    "duration_ms": s.duration_ms,
                    "details": s.details,
                    "error": s.error
                }
                for s in self.steps
            ]
        }


class VBIDebugTracer:
    """
    Comprehensive VBI Debug Tracer

    Provides detailed step-by-step tracing of voice biometric operations
    with timing, status tracking, and diagnostic information.
    """

    _instance = None
    _lock = asyncio.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._initialized = True
        self.active_traces: Dict[str, VBITrace] = {}
        self.completed_traces: List[VBITrace] = []
        self.max_completed_traces = 100  # Keep last 100 traces
        self.trace_counter = 0

        # Performance thresholds (configurable)
        self.thresholds = {
            VBIStage.AUDIO_RECEIVE: 100,      # 100ms
            VBIStage.AUDIO_DECODE: 200,       # 200ms
            VBIStage.AUDIO_PREPROCESS: 300,   # 300ms
            VBIStage.ECAPA_EXTRACT: 2000,     # 2s (cloud can be slow)
            VBIStage.SPEAKER_VERIFY: 500,     # 500ms
            VBIStage.DECISION: 50,            # 50ms
            VBIStage.UNLOCK_EXECUTE: 3000,    # 3s for unlock
            VBIStage.RESPONSE_BUILD: 50,      # 50ms
            VBIStage.WEBSOCKET_SEND: 100,     # 100ms
        }

        # Dynamic timeout multipliers based on system state
        self.timeout_multipliers = {
            "cold_start": 3.0,
            "warm": 1.0,
            "degraded": 2.0
        }

        self.system_state = "cold_start"
        self.successful_requests = 0

        logger.info("=" * 70)
        logger.info("VBI DEBUG TRACER INITIALIZED")
        logger.info("=" * 70)
        logger.info(f"   Thresholds: {len(self.thresholds)} stages configured")
        logger.info(f"   Max traces: {self.max_completed_traces}")
        logger.info("=" * 70)

    def generate_trace_id(self) -> str:
        """Generate unique trace ID"""
        self.trace_counter += 1
        return f"vbi_{int(time.time())}_{self.trace_counter:04d}"

    def start_trace(self, command: str, audio_size: int = 0,
                    sample_rate: int = 0, mime_type: str = "") -> str:
        """Start a new VBI trace"""
        trace_id = self.generate_trace_id()

        trace = VBITrace(
            trace_id=trace_id,
            command=command,
            start_time=time.time(),
            audio_size_bytes=audio_size,
            sample_rate=sample_rate,
            mime_type=mime_type
        )

        self.active_traces[trace_id] = trace

        logger.info("=" * 70)
        logger.info(f"[VBI-TRACE] {trace_id} STARTED")
        logger.info("=" * 70)
        logger.info(f"   Command: '{command}'")
        logger.info(f"   Audio: {audio_size} bytes, {sample_rate}Hz, {mime_type}")
        logger.info(f"   System State: {self.system_state}")
        logger.info("=" * 70)

        return trace_id

    @asynccontextmanager
    async def trace_step(self, trace_id: str, stage: VBIStage):
        """Context manager for tracing a step"""
        trace = self.active_traces.get(trace_id)
        if not trace:
            logger.warning(f"[VBI-TRACE] Unknown trace_id: {trace_id}")
            yield None
            return

        step = VBITraceStep(
            stage=stage,
            status=VBIStatus.IN_PROGRESS,
            start_time=time.time()
        )
        trace.steps.append(step)

        threshold = self.thresholds.get(stage, 1000)
        multiplier = self.timeout_multipliers.get(self.system_state, 1.0)
        effective_threshold = threshold * multiplier

        logger.info(f"[VBI-TRACE] {trace_id} | {stage.value.upper()} | STARTED (threshold: {effective_threshold:.0f}ms)")

        try:
            yield step

            step.complete(VBIStatus.SUCCESS)

            # Log with appropriate level based on duration
            if step.duration_ms > effective_threshold:
                logger.warning(
                    f"[VBI-TRACE] {trace_id} | {stage.value.upper()} | SLOW: {step.duration_ms:.1f}ms "
                    f"(threshold: {effective_threshold:.0f}ms)"
                )
            else:
                logger.info(
                    f"[VBI-TRACE] {trace_id} | {stage.value.upper()} | OK: {step.duration_ms:.1f}ms"
                )

        except asyncio.TimeoutError as e:
            step.complete(VBIStatus.TIMEOUT, error=f"Timeout after {effective_threshold}ms")
            logger.error(
                f"[VBI-TRACE] {trace_id} | {stage.value.upper()} | TIMEOUT after {step.duration_ms:.1f}ms"
            )
            raise

        except Exception as e:
            step.complete(VBIStatus.FAILED, error=str(e))
            logger.error(
                f"[VBI-TRACE] {trace_id} | {stage.value.upper()} | FAILED: {e}\n"
                f"   Traceback: {traceback.format_exc()}"
            )
            raise

    def add_step_details(self, trace_id: str, stage: VBIStage, details: Dict[str, Any]):
        """Add details to the current step"""
        trace = self.active_traces.get(trace_id)
        if not trace:
            return

        for step in reversed(trace.steps):
            if step.stage == stage:
                step.details.update(details)
                logger.debug(f"[VBI-TRACE] {trace_id} | {stage.value} | Details: {details}")
                break

    def complete_trace(self, trace_id: str, status: VBIStatus,
                       speaker_confidence: float = 0.0, speaker_name: str = ""):
        """Complete a VBI trace"""
        trace = self.active_traces.pop(trace_id, None)
        if not trace:
            logger.warning(f"[VBI-TRACE] Cannot complete unknown trace: {trace_id}")
            return

        trace.final_status = status
        trace.speaker_confidence = speaker_confidence
        trace.speaker_name = speaker_name
        trace.total_duration_ms = (time.time() - trace.start_time) * 1000

        # Add to completed traces
        self.completed_traces.append(trace)
        if len(self.completed_traces) > self.max_completed_traces:
            self.completed_traces.pop(0)

        # Update system state based on success
        if status == VBIStatus.SUCCESS:
            self.successful_requests += 1
            if self.successful_requests >= 3:
                self.system_state = "warm"

        # Log summary
        logger.info("=" * 70)
        logger.info(f"[VBI-TRACE] {trace_id} COMPLETED")
        logger.info("=" * 70)
        logger.info(f"   Status: {status.value.upper()}")
        logger.info(f"   Total Duration: {trace.total_duration_ms:.1f}ms")
        logger.info(f"   Speaker: {speaker_name} ({speaker_confidence:.1%} confidence)")
        logger.info(f"   Steps: {len(trace.steps)}")

        for step in trace.steps:
            status_icon = {
                VBIStatus.SUCCESS: "",
                VBIStatus.FAILED: "",
                VBIStatus.TIMEOUT: "",
                VBIStatus.SKIPPED: ""
            }.get(step.status, "")

            logger.info(
                f"      {status_icon} {step.stage.value}: {step.duration_ms:.1f}ms "
                f"[{step.status.value}]"
            )
            if step.error:
                logger.info(f"         Error: {step.error}")

        logger.info("=" * 70)

        # Write to trace log file for analysis
        self._write_trace_to_file(trace)

    def _write_trace_to_file(self, trace: VBITrace):
        """Write trace to log file for later analysis"""
        log_dir = os.path.expanduser("~/.jarvis/logs")
        os.makedirs(log_dir, exist_ok=True)

        log_file = os.path.join(log_dir, "vbi_traces.jsonl")
        try:
            with open(log_file, "a") as f:
                f.write(json.dumps(trace.to_dict()) + "\n")
        except Exception as e:
            logger.warning(f"Failed to write trace to file: {e}")

    def get_recent_traces(self, limit: int = 10) -> List[Dict]:
        """Get recent completed traces"""
        return [t.to_dict() for t in self.completed_traces[-limit:]]

    def get_performance_stats(self) -> Dict[str, Any]:
        """Get performance statistics"""
        if not self.completed_traces:
            return {"message": "No traces available"}

        stats = {
            "total_traces": len(self.completed_traces),
            "system_state": self.system_state,
            "successful_requests": self.successful_requests,
            "stages": {}
        }

        for stage in VBIStage:
            durations = []
            for trace in self.completed_traces:
                for step in trace.steps:
                    if step.stage == stage and step.duration_ms is not None:
                        durations.append(step.duration_ms)

            if durations:
                stats["stages"][stage.value] = {
                    "avg_ms": sum(durations) / len(durations),
                    "min_ms": min(durations),
                    "max_ms": max(durations),
                    "count": len(durations)
                }

        return stats


class ECAPAPreWarmer:
    """
    ECAPA Pre-Warmer v2.0 - Ultra-robust async warmup with proper timeouts.

    Features:
    - Parallel endpoint discovery
    - Stage-based warmup with individual timeouts
    - Circuit breaker to prevent getting stuck
    - Graceful fallback when cloud is unavailable
    - Non-blocking health monitoring
    - Dynamic configuration via environment variables

    This class handles:
    1. Pre-warming ECAPA at startup (during start_system.py)
    2. Health monitoring to ensure ECAPA stays warm
    3. Auto-recovery if ECAPA becomes unavailable
    """

    _instance = None
    _lock = asyncio.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._singleton_initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, '_singleton_initialized', False):
            return

        self._singleton_initialized = True
        self.is_warm = False
        self.last_warmup_time: Optional[float] = None
        self.warmup_duration_ms: Optional[float] = None
        self.cloud_endpoint: Optional[str] = None
        
        # Configurable timeouts (from environment with sensible defaults)
        self.health_check_interval = int(os.environ.get("ECAPA_HEALTH_CHECK_INTERVAL", "60"))
        self.warmup_timeout = int(os.environ.get("ECAPA_WARMUP_TIMEOUT", "60"))  # Total warmup timeout
        self.stage_timeout = int(os.environ.get("ECAPA_STAGE_TIMEOUT", "30"))    # Per-stage timeout
        self.auto_rewarm_threshold = int(os.environ.get("ECAPA_REWARM_THRESHOLD", "300"))  # 5 minutes

        # Circuit breaker state
        self._consecutive_failures = 0
        self._max_failures = 3
        self._circuit_open_until: Optional[float] = None
        
        # Warmup state
        self._warmup_in_progress = False
        self._warmup_task: Optional[asyncio.Task] = None

        # Test embedding for warming (0.5 seconds of low noise at 16kHz)
        self._warmup_audio = self._generate_warmup_audio()

        # Background task handle
        self._health_task: Optional[asyncio.Task] = None

        # Stats
        self._stats = {
            "warmup_attempts": 0,
            "warmup_successes": 0,
            "warmup_failures": 0,
            "circuit_opens": 0,
        }

        logger.info("=" * 70)
        logger.info("ECAPA PRE-WARMER v2.0 INITIALIZED")
        logger.info(f"   Warmup Timeout: {self.warmup_timeout}s")
        logger.info(f"   Stage Timeout: {self.stage_timeout}s")
        logger.info(f"   Rewarm Threshold: {self.auto_rewarm_threshold}s")
        logger.info("=" * 70)

    def _is_circuit_open(self) -> bool:
        """Check if circuit breaker is open."""
        if self._circuit_open_until is None:
            return False
        if time.time() >= self._circuit_open_until:
            self._circuit_open_until = None
            self._consecutive_failures = 0
            logger.info("[ECAPA-PREWARM] 🔌 Circuit breaker recovered")
            return False
        return True

    def _record_failure(self):
        """Record a failure and potentially open circuit."""
        self._consecutive_failures += 1
        self._stats["warmup_failures"] += 1
        
        if self._consecutive_failures >= self._max_failures:
            self._circuit_open_until = time.time() + 120  # 2 minutes
            self._stats["circuit_opens"] += 1
            logger.warning(
                f"[ECAPA-PREWARM] 🔴 Circuit breaker OPEN for 120s "
                f"(failures: {self._consecutive_failures})"
            )

    def _record_success(self):
        """Record success and reset circuit breaker."""
        self._consecutive_failures = 0
        self._circuit_open_until = None
        self._stats["warmup_successes"] += 1

    def _generate_warmup_audio(self) -> bytes:
        """Generate a small audio sample for warming up ECAPA"""
        import base64
        import struct

        # Generate 0.5 seconds of low-amplitude noise at 16kHz
        sample_rate = 16000
        duration = 0.5
        num_samples = int(sample_rate * duration)

        # Create low-amplitude samples (near silence but not exactly zero)
        import random
        samples = [random.randint(-100, 100) for _ in range(num_samples)]

        # Pack as 16-bit PCM
        pcm_data = struct.pack(f'{num_samples}h', *samples)

        return base64.b64encode(pcm_data).decode('utf-8')

    async def warmup(self, force: bool = False) -> Dict[str, Any]:
        """
        Pre-warm ECAPA embedding extraction with robust async handling.

        v2.0.0 Features:
        - Each stage has individual timeout protection
        - Circuit breaker prevents infinite retries
        - Non-blocking with proper cancellation support
        - Graceful degradation if cloud unavailable
        - Detailed logging at each stage

        This should be called at startup to ensure ECAPA is ready
        before any unlock requests come in.
        """
        self._stats["warmup_attempts"] += 1
        
        # Check circuit breaker
        if self._is_circuit_open():
            logger.warning("[ECAPA-PREWARM] ⚠️ Circuit breaker OPEN - skipping warmup")
            return {
                "status": "circuit_open",
                "message": "Warmup blocked by circuit breaker due to recent failures",
                "retry_after_seconds": max(0, (self._circuit_open_until or 0) - time.time())
            }

        async with self._lock:
            # Prevent concurrent warmups
            if self._warmup_in_progress:
                logger.info("[ECAPA-PREWARM] Warmup already in progress")
                return {"status": "in_progress"}

            if self.is_warm and not force:
                time_since_warmup = time.time() - (self.last_warmup_time or 0)
                if time_since_warmup < self.auto_rewarm_threshold:
                    logger.info(f"[ECAPA-PREWARM] Already warm ({time_since_warmup:.0f}s ago)")
                    return {
                        "status": "already_warm",
                        "last_warmup_ms": self.warmup_duration_ms,
                        "time_since_warmup_s": time_since_warmup
                    }

            self._warmup_in_progress = True

        try:
            logger.info("=" * 70)
            logger.info("[ECAPA-PREWARM] 🔥 STARTING PRE-WARM SEQUENCE v2.0.0")
            logger.info("=" * 70)
            logger.info(f"   Total Timeout: {self.warmup_timeout}s")
            logger.info(f"   Stage Timeout: {self.stage_timeout}s")

            start_time = time.time()
            result = {"status": "unknown", "stages": [], "version": "2.0.0"}

            try:
                # Wrap entire warmup in a timeout
                warmup_result = await asyncio.wait_for(
                    self._execute_warmup_stages(result),
                    timeout=self.warmup_timeout
                )
                
                if warmup_result.get("status") == "success":
                    self._record_success()
                else:
                    self._record_failure()
                    
                return warmup_result

            except asyncio.TimeoutError:
                self.warmup_duration_ms = (time.time() - start_time) * 1000
                result["status"] = "timeout"
                result["total_duration_ms"] = self.warmup_duration_ms
                result["error"] = f"Total warmup timeout after {self.warmup_timeout}s"
                logger.error(f"[ECAPA-PREWARM] ⏱️ TIMEOUT after {self.warmup_duration_ms:.0f}ms")
                logger.error(f"   Completed stages: {[s['stage'] for s in result.get('stages', [])]}")
                self._record_failure()
                return result

            except asyncio.CancelledError:
                logger.warning("[ECAPA-PREWARM] Warmup was cancelled")
                result["status"] = "cancelled"
                self._record_failure()
                return result

            except Exception as e:
                self.warmup_duration_ms = (time.time() - start_time) * 1000
                result["status"] = "failed"
                result["error"] = str(e)
                result["total_duration_ms"] = self.warmup_duration_ms
                logger.error(f"[ECAPA-PREWARM] ❌ FAILED: {e}")
                logger.debug(f"   Traceback: {traceback.format_exc()}")
                self._record_failure()
                return result

        finally:
            self._warmup_in_progress = False

    async def _execute_warmup_stages(self, result: Dict) -> Dict:
        """Execute warmup stages with individual timeouts."""
        start_time = time.time()

        # Stage 1: Find cloud endpoint (with timeout)
        stage_start = time.time()
        try:
            logger.info("[ECAPA-PREWARM] 📡 Stage 1: Finding cloud endpoint...")
            endpoint = await asyncio.wait_for(
                self._find_cloud_endpoint(),
                timeout=self.stage_timeout
            )
            result["stages"].append({
                "stage": "find_endpoint",
                "status": "success" if endpoint else "no_endpoint",
                "duration_ms": (time.time() - stage_start) * 1000,
                "endpoint": endpoint
            })
        except asyncio.TimeoutError:
            result["stages"].append({
                "stage": "find_endpoint",
                "status": "timeout",
                "duration_ms": (time.time() - stage_start) * 1000
            })
            logger.warning(f"[ECAPA-PREWARM] ⏱️ Stage 1 timeout after {self.stage_timeout}s")
            endpoint = None
        except Exception as e:
            result["stages"].append({
                "stage": "find_endpoint",
                "status": "error",
                "error": str(e),
                "duration_ms": (time.time() - stage_start) * 1000
            })
            logger.warning(f"[ECAPA-PREWARM] ❌ Stage 1 error: {e}")
            endpoint = None

        if not endpoint:
            # Try fallback endpoints
            logger.info("[ECAPA-PREWARM] 🔄 Trying fallback endpoint discovery...")
            endpoint = await self._try_fallback_endpoints()
            
        if not endpoint:
            result["status"] = "no_endpoint"
            result["total_duration_ms"] = (time.time() - start_time) * 1000
            logger.warning("[ECAPA-PREWARM] ⚠️ No cloud ECAPA endpoint found - voice unlock may be slower")
            return result

        self.cloud_endpoint = endpoint
        logger.info(f"[ECAPA-PREWARM] ✅ Found endpoint: {endpoint}")

        # Stage 2: Send warmup request (with timeout)
        stage_start = time.time()
        try:
            logger.info("[ECAPA-PREWARM] 🔄 Stage 2: Extracting warmup embedding...")
            warmup_result = await asyncio.wait_for(
                self._send_warmup_request(endpoint),
                timeout=self.stage_timeout
            )
            result["stages"].append({
                "stage": "warmup_request",
                "status": "success",
                "duration_ms": (time.time() - stage_start) * 1000,
                "embedding_size": warmup_result.get("embedding_size", 0)
            })
            logger.info(f"[ECAPA-PREWARM] ✅ Warmup embedding: {warmup_result.get('embedding_size', 0)} dimensions")
        except asyncio.TimeoutError:
            result["stages"].append({
                "stage": "warmup_request",
                "status": "timeout",
                "duration_ms": (time.time() - stage_start) * 1000
            })
            logger.warning(f"[ECAPA-PREWARM] ⏱️ Stage 2 timeout after {self.stage_timeout}s")
            # Mark as partially warm - endpoint was found
            self.is_warm = False
            self.last_warmup_time = time.time()
            result["status"] = "partial"
            result["message"] = "Endpoint found but embedding extraction timed out"
            result["total_duration_ms"] = (time.time() - start_time) * 1000
            return result
        except Exception as e:
            result["stages"].append({
                "stage": "warmup_request",
                "status": "error",
                "error": str(e),
                "duration_ms": (time.time() - stage_start) * 1000
            })
            logger.warning(f"[ECAPA-PREWARM] ❌ Stage 2 error: {e}")
            result["status"] = "partial"
            result["message"] = f"Endpoint found but embedding failed: {e}"
            result["total_duration_ms"] = (time.time() - start_time) * 1000
            return result

        # Stage 3: Verify embedding quality (quick, no timeout needed)
        stage_start = time.time()
        is_valid = self._verify_embedding(warmup_result.get("embedding", []))
        result["stages"].append({
            "stage": "verify_embedding",
            "status": "valid" if is_valid else "invalid",
            "duration_ms": (time.time() - stage_start) * 1000,
            "is_valid": is_valid
        })

        if not is_valid:
            logger.warning("[ECAPA-PREWARM] ⚠️ Embedding validation failed but proceeding")
            # Still mark as warm - embedding extraction worked

        # Mark as warm
        self.is_warm = True
        self.last_warmup_time = time.time()
        self.warmup_duration_ms = (time.time() - start_time) * 1000

        result["status"] = "success"
        result["total_duration_ms"] = self.warmup_duration_ms

        logger.info("=" * 70)
        logger.info(f"[ECAPA-PREWARM] 🎉 PRE-WARM COMPLETE in {self.warmup_duration_ms:.0f}ms")
        logger.info(f"[ECAPA-PREWARM] 🚀 Voice unlock commands will now be INSTANT!")
        logger.info("=" * 70)

        # Start health monitoring in background (non-blocking)
        if self._health_task is None or self._health_task.done():
            self._health_task = asyncio.create_task(
                self._health_monitor(),
                name="ecapa_health_monitor"
            )

        return result

    async def _try_fallback_endpoints(self) -> Optional[str]:
        """Try fallback methods to find an endpoint."""
        try:
            # Try environment variable
            endpoint = os.environ.get("CLOUD_ECAPA_ENDPOINT")
            if endpoint:
                logger.info(f"[ECAPA-PREWARM] Using env endpoint: {endpoint}")
                return endpoint

            # Try default Cloud Run endpoints
            default_endpoints = [
                os.environ.get("JARVIS_ML_ENDPOINT"),
                "https://jarvis-ml-888774109345.us-central1.run.app",
            ]

            import aiohttp
            for endpoint in default_endpoints:
                if not endpoint:
                    continue
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            f"{endpoint}/health",
                            timeout=aiohttp.ClientTimeout(total=5)
                        ) as resp:
                            if resp.status == 200:
                                logger.info(f"[ECAPA-PREWARM] Fallback endpoint found: {endpoint}")
                                return endpoint
                except Exception:
                    continue

            return None

        except Exception as e:
            logger.debug(f"[ECAPA-PREWARM] Fallback endpoint search failed: {e}")
            return None

    async def _find_cloud_endpoint(self) -> Optional[str]:
        """Find available cloud ECAPA endpoint with parallel discovery."""
        import aiohttp
        
        # Collect potential endpoints to check
        endpoints_to_check = []
        
        # From environment variables (highest priority)
        for env_var in ["CLOUD_ECAPA_ENDPOINT", "JARVIS_ML_ENDPOINT", "ML_SERVICE_URL"]:
            env_endpoint = os.environ.get(env_var)
            if env_endpoint:
                endpoints_to_check.append(env_endpoint)
        
        # Try to get from CloudECAPAClient (without blocking)
        try:
            from voice_unlock.cloud_ecapa_client import CloudECAPAClient
            client = CloudECAPAClient()
            
            if hasattr(client, '_healthy_endpoint') and client._healthy_endpoint:
                endpoints_to_check.insert(0, client._healthy_endpoint)
            
            if hasattr(client, '_endpoints') and client._endpoints:
                for ep in client._endpoints:
                    if ep not in endpoints_to_check:
                        endpoints_to_check.append(ep)
                        
        except Exception as e:
            logger.debug(f"[ECAPA-PREWARM] Could not get endpoints from client: {e}")
        
        # Add default Cloud Run endpoint
        default_endpoint = "https://jarvis-ml-888774109345.us-central1.run.app"
        if default_endpoint not in endpoints_to_check:
            endpoints_to_check.append(default_endpoint)
        
        if not endpoints_to_check:
            return None
        
        logger.info(f"[ECAPA-PREWARM] Checking {len(endpoints_to_check)} potential endpoint(s)...")
        
        # Check endpoints in parallel with short timeout
        async def check_endpoint(endpoint: str) -> Optional[str]:
            try:
                async with aiohttp.ClientSession() as session:
                    health_url = f"{endpoint.rstrip('/')}/health"
                    async with session.get(
                        health_url,
                        timeout=aiohttp.ClientTimeout(total=5)
                    ) as resp:
                        if resp.status == 200:
                            return endpoint
                        elif resp.status < 500:
                            # Endpoint exists but might not be ready
                            return endpoint
            except Exception:
                pass
            return None
        
        # Check all endpoints in parallel
        results = await asyncio.gather(
            *[check_endpoint(ep) for ep in endpoints_to_check],
            return_exceptions=True
        )
        
        # Return first healthy endpoint
        for result in results:
            if isinstance(result, str) and result:
                return result
        
        # If no healthy endpoint, return first one (might just be cold)
        return endpoints_to_check[0] if endpoints_to_check else None

    async def _send_warmup_request(self, endpoint: str) -> Dict[str, Any]:
        """
        Send warmup embedding request with robust timeout handling.

        v2.0.0 Enhancements:
        - Individual timeout for each operation
        - Parallel initialization where possible
        - Graceful fallback to raw HTTP
        - No more hanging on slow responses
        """
        # Stage timeout for individual operations (shorter than main stage timeout)
        op_timeout = min(15, self.stage_timeout // 2)
        
        try:
            # Try CloudECAPAClient first
            client = await self._get_cloud_client_with_timeout(op_timeout)
            
            if client:
                return await self._warmup_via_client(client, endpoint, op_timeout)
            else:
                # Fallback to raw HTTP
                logger.info("[ECAPA-PREWARM] 🔄 Using raw HTTP fallback...")
                return await self._send_warmup_request_raw(endpoint)

        except asyncio.TimeoutError:
            logger.warning(f"[ECAPA-PREWARM] ⏱️ CloudECAPAClient timeout, trying raw HTTP...")
            return await self._send_warmup_request_raw(endpoint)

        except ImportError as e:
            logger.warning(f"[ECAPA-PREWARM] CloudECAPAClient not available: {e}")
            return await self._send_warmup_request_raw(endpoint)

        except Exception as e:
            logger.warning(f"[ECAPA-PREWARM] CloudECAPAClient failed ({e}), trying raw HTTP...")
            return await self._send_warmup_request_raw(endpoint)

    async def _get_cloud_client_with_timeout(self, timeout: float):
        """Get CloudECAPAClient with timeout protection."""
        try:
            from voice_unlock.cloud_ecapa_client import get_cloud_ecapa_client
            
            client = await asyncio.wait_for(
                get_cloud_ecapa_client(),
                timeout=timeout
            )
            
            if client and not client._initialized:
                logger.info("[ECAPA-PREWARM] Initializing CloudECAPAClient...")
                init_result = await asyncio.wait_for(
                    client.initialize(),
                    timeout=timeout
                )
                # initialize() returns True/False or dict
                if isinstance(init_result, dict) and not init_result.get("success", True):
                    logger.warning(f"[ECAPA-PREWARM] Client init returned: {init_result}")
            
            return client
            
        except asyncio.TimeoutError:
            logger.warning("[ECAPA-PREWARM] CloudECAPAClient initialization timeout")
            return None
        except Exception as e:
            logger.debug(f"[ECAPA-PREWARM] CloudECAPAClient error: {e}")
            return None

    async def _warmup_via_client(self, client, endpoint: str, op_timeout: float) -> Dict[str, Any]:
        """Perform warmup using CloudECAPAClient with timeout protection."""
        import base64
        import struct

        # Check if ECAPA is ready (with shorter timeout for warmup)
        logger.info("[ECAPA-PREWARM] 🔍 Checking if ECAPA model is ready...")
        
        try:
            ready_result = await asyncio.wait_for(
                client.wait_for_ecapa_ready(
                    endpoint=endpoint,
                    timeout=op_timeout,  # Use our shorter timeout
                    poll_interval=2.0,   # Poll every 2 seconds
                    log_progress=True
                ),
                timeout=op_timeout + 5  # Slightly longer outer timeout
            )
            
            if ready_result.get("ready"):
                logger.info(f"[ECAPA-PREWARM] ✅ ECAPA ready (waited {ready_result.get('elapsed_seconds', 0):.1f}s)")
            else:
                logger.warning(f"[ECAPA-PREWARM] ⚠️ ECAPA not confirmed ready: {ready_result.get('error')}")
                # Continue anyway - try to extract embedding
                
        except asyncio.TimeoutError:
            logger.warning(f"[ECAPA-PREWARM] ⏱️ ECAPA ready check timeout - proceeding anyway")

        # Prepare warmup audio
        warmup_audio_bytes = base64.b64decode(self._warmup_audio)
        num_samples = len(warmup_audio_bytes) // 2
        pcm_samples = struct.unpack(f'{num_samples}h', warmup_audio_bytes)
        float_samples = [s / 32768.0 for s in pcm_samples]
        float_audio = struct.pack(f'{len(float_samples)}f', *float_samples)

        # Extract embedding with timeout
        logger.info("[ECAPA-PREWARM] 📤 Extracting warmup embedding...")
        
        embedding = await asyncio.wait_for(
            client.extract_embedding(
                audio_data=float_audio,
                sample_rate=16000,
                format="float32",
                use_cache=False
            ),
            timeout=op_timeout
        )

        if embedding is None:
            raise Exception("Embedding extraction returned None")

        embedding_list = embedding.tolist() if hasattr(embedding, 'tolist') else list(embedding)
        
        logger.info(f"[ECAPA-PREWARM] ✅ Warmup embedding: {len(embedding_list)} dimensions")

        return {
            "embedding": embedding_list,
            "embedding_size": len(embedding_list),
            "endpoint_path": "/api/ml/speaker_embedding",
            "client_version": getattr(client, 'VERSION', 'unknown'),
            "method": "cloud_ecapa_client"
        }

    async def _send_warmup_request_raw(self, endpoint: str) -> Dict[str, Any]:
        """Fallback raw HTTP warmup request (if CloudECAPAClient not available)"""
        import aiohttp

        async with aiohttp.ClientSession() as session:
            payload = {
                "audio_data": self._warmup_audio,
                "sample_rate": 16000,
                "format": "pcm"
            }

            # Try multiple possible endpoint paths (Cloud Run uses /api/ml/speaker_embedding)
            endpoint_paths = [
                "/api/ml/speaker_embedding",
                "/speaker_embedding",
                "/extract_embedding",
            ]

            last_error = None
            for path in endpoint_paths:
                full_url = f"{endpoint.rstrip('/')}{path}"
                logger.debug(f"[ECAPA-PREWARM] Trying warmup endpoint: {full_url}")

                try:
                    async with session.post(
                        full_url,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=self.warmup_timeout)
                    ) as resp:
                        if resp.status == 200:
                            result = await resp.json()
                            embedding = result.get("embedding", result.get("speaker_embedding", []))
                            return {
                                "embedding": embedding,
                                "embedding_size": len(embedding),
                                "endpoint_path": path
                            }
                        elif resp.status == 404:
                            # Try next endpoint
                            logger.debug(f"[ECAPA-PREWARM] Endpoint {path} returned 404, trying next...")
                            continue
                        else:
                            text = await resp.text()
                            last_error = f"Warmup request failed: {resp.status} - {text}"
                            continue

                except aiohttp.ClientError as e:
                    last_error = str(e)
                    continue

            # If we get here, all endpoints failed
            raise Exception(last_error or "All warmup endpoints failed")

    def _verify_embedding(self, embedding: List[float]) -> bool:
        """Verify embedding is valid"""
        if not embedding:
            return False

        # Check dimension (ECAPA-TDNN should be 192)
        if len(embedding) != 192:
            logger.warning(f"[ECAPA-PREWARM] Unexpected embedding size: {len(embedding)}")
            # Don't fail on this, some models may have different sizes

        # Check for NaN or Inf values
        import math
        for val in embedding:
            if math.isnan(val) or math.isinf(val):
                return False

        # Check for all zeros (indicates failed extraction)
        if all(v == 0 for v in embedding):
            return False

        return True

    async def _health_monitor(self):
        """Background task to monitor ECAPA health"""
        logger.info("[ECAPA-PREWARM] Health monitor started")

        while True:
            try:
                await asyncio.sleep(self.health_check_interval)

                # Check if we need to re-warm
                if self.last_warmup_time:
                    time_since = time.time() - self.last_warmup_time
                    if time_since > self.auto_rewarm_threshold:
                        logger.info(f"[ECAPA-PREWARM] Auto re-warming (last warmup {time_since:.0f}s ago)")
                        await self.warmup(force=True)

            except asyncio.CancelledError:
                logger.info("[ECAPA-PREWARM] Health monitor stopped")
                break
            except Exception as e:
                logger.error(f"[ECAPA-PREWARM] Health monitor error: {e}")

    def get_status(self) -> Dict[str, Any]:
        """Get comprehensive pre-warmer status including circuit breaker."""
        return {
            "version": "2.0.0",
            "is_warm": self.is_warm,
            "last_warmup_time": datetime.fromtimestamp(self.last_warmup_time).isoformat() if self.last_warmup_time else None,
            "warmup_duration_ms": self.warmup_duration_ms,
            "cloud_endpoint": self.cloud_endpoint,
            "time_since_warmup_s": time.time() - self.last_warmup_time if self.last_warmup_time else None,
            "warmup_in_progress": self._warmup_in_progress,
            "circuit_breaker": {
                "is_open": self._is_circuit_open(),
                "consecutive_failures": self._consecutive_failures,
                "max_failures": self._max_failures,
                "open_until": self._circuit_open_until,
            },
            "stats": self._stats,
            "config": {
                "warmup_timeout": self.warmup_timeout,
                "stage_timeout": self.stage_timeout,
                "auto_rewarm_threshold": self.auto_rewarm_threshold,
            }
        }


class VBIPipelineOrchestrator:
    """
    VBI Pipeline Orchestrator - Manages the entire voice unlock flow

    This orchestrator:
    1. Uses the debug tracer for detailed logging
    2. Ensures ECAPA is pre-warmed before processing
    3. Handles timeouts gracefully
    4. Provides automatic retry with backoff
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._initialized = True
        self.tracer = VBIDebugTracer()
        self.prewarmer = ECAPAPreWarmer()

        # Dynamic timeouts (in seconds)
        # NOTE: Warmup should happen at STARTUP, not during user requests!
        # The warmup_timeout here is a FALLBACK if startup warmup failed
        self.timeouts = {
            "warmup": float(os.environ.get("VBI_WARMUP_TIMEOUT", "30")),  # Separate warmup timeout (fallback)
            "audio_processing": float(os.environ.get("VBI_AUDIO_TIMEOUT", "5")),
            "ecapa_extraction": float(os.environ.get("VBI_ECAPA_TIMEOUT", "30")),  # Increased for cold endpoints
            "speaker_verification": float(os.environ.get("VBI_VERIFY_TIMEOUT", "5")),
            "unlock_execution": float(os.environ.get("VBI_UNLOCK_TIMEOUT", "10")),
            "total_pipeline": float(os.environ.get("VBI_TOTAL_TIMEOUT", "60"))  # Increased for reliability
        }

        # Retry configuration
        self.max_retries = int(os.environ.get("VBI_MAX_RETRIES", "2"))
        self.retry_backoff = float(os.environ.get("VBI_RETRY_BACKOFF", "0.5"))

        logger.info("=" * 70)
        logger.info("VBI PIPELINE ORCHESTRATOR INITIALIZED")
        logger.info("=" * 70)
        logger.info(f"   Timeouts: {self.timeouts}")
        logger.info(f"   Max Retries: {self.max_retries}")
        logger.info("=" * 70)

    async def ensure_ready(self) -> bool:
        """Ensure the VBI pipeline is ready for requests"""
        if not self.prewarmer.is_warm:
            logger.info("[VBI-ORCH] ECAPA not warm, warming up...")
            result = await self.prewarmer.warmup()
            return result.get("status") == "success"
        return True

    async def process_voice_unlock(
        self,
        command: str,
        audio_data: Optional[str] = None,
        sample_rate: int = 16000,
        mime_type: str = "audio/webm",
        progress_callback: Optional[Callable[[Dict[str, Any]], Any]] = None
    ) -> Dict[str, Any]:
        """
        Process a voice unlock request with full tracing and real-time progress

        Args:
            command: The voice command text
            audio_data: Base64 encoded audio data
            sample_rate: Audio sample rate
            mime_type: Audio MIME type
            progress_callback: Optional async callback for real-time progress updates
                               Receives dict with: stage, progress, message, details

        Returns:
            Dict with response, success status, and trace info
        """
        # VBI Pipeline Stages with progress percentages and display names
        VBI_STAGES = {
            "warmup": {"progress": 5, "name": "Warming Up", "icon": "fire"},
            "audio_receive": {"progress": 15, "name": "Receiving Audio", "icon": "microphone"},
            "audio_decode": {"progress": 25, "name": "Decoding Audio", "icon": "waveform"},
            "audio_preprocess": {"progress": 40, "name": "Preprocessing", "icon": "filter"},
            "ecapa_extract": {"progress": 60, "name": "Extracting Voiceprint", "icon": "fingerprint"},
            "speaker_verify": {"progress": 75, "name": "Verifying Speaker", "icon": "user-check"},
            "decision": {"progress": 85, "name": "Making Decision", "icon": "brain"},
            "unlock_execute": {"progress": 95, "name": "Unlocking Screen", "icon": "unlock"},
            "complete": {"progress": 100, "name": "Complete", "icon": "check-circle"},
        }

        async def send_progress(stage: str, status: str = "in_progress", details: Dict = None, error: str = None):
            """Send progress update via callback if provided"""
            if progress_callback:
                stage_info = VBI_STAGES.get(stage, {"progress": 0, "name": stage, "icon": "cog"})
                progress_data = {
                    "type": "vbi_progress",
                    "stage": stage,
                    "stage_name": stage_info["name"],
                    "stage_icon": stage_info["icon"],
                    "progress": stage_info["progress"],
                    "status": status,  # "in_progress", "success", "failed"
                    "details": details or {},
                    "error": error,
                    "trace_id": trace_id if 'trace_id' in dir() else None,
                    "timestamp": time.time()
                }
                try:
                    if asyncio.iscoroutinefunction(progress_callback):
                        await progress_callback(progress_data)
                    else:
                        progress_callback(progress_data)
                except Exception as cb_err:
                    logger.warning(f"[VBI-ORCH] Progress callback error: {cb_err}")

        # Start trace
        audio_size = len(audio_data) if audio_data else 0
        trace_id = self.tracer.start_trace(
            command=command,
            audio_size=audio_size,
            sample_rate=sample_rate,
            mime_type=mime_type
        )

        result = {
            "trace_id": trace_id,
            "command": command,
            "success": False,
            "response": "",
            "speaker_name": "",
            "confidence": 0.0
        }

        try:
            # Check ECAPA warmup status
            # NOTE: Warmup should have happened at STARTUP via component_warmup_config.py
            # This is a FALLBACK if startup warmup failed - we try but proceed anyway
            if not self.prewarmer.is_warm:
                await send_progress("warmup", "in_progress", {"message": "Initializing voice verification..."})
                logger.warning(f"[VBI-ORCH] {trace_id} | ECAPA not pre-warmed at startup, attempting fallback warmup...")
                
                try:
                    warmup_result = await asyncio.wait_for(
                        self.prewarmer.warmup(),
                        timeout=self.timeouts["warmup"]  # Use dedicated warmup timeout
                    )
                    
                    if warmup_result.get("status") == "success":
                        await send_progress("warmup", "success", {"message": "System ready"})
                        logger.info(f"[VBI-ORCH] {trace_id} | Fallback warmup succeeded")
                    else:
                        # Warmup didn't succeed but we'll try verification anyway
                        await send_progress("warmup", "skipped", {"message": "Proceeding with verification..."})
                        logger.warning(f"[VBI-ORCH] {trace_id} | Warmup status: {warmup_result.get('status')}, proceeding anyway")
                        
                except asyncio.TimeoutError:
                    # Warmup timed out - proceed anyway, the extraction might still work
                    await send_progress("warmup", "skipped", {"message": "Proceeding with verification..."})
                    logger.warning(f"[VBI-ORCH] {trace_id} | Warmup timeout, proceeding with verification anyway")
                    
            else:
                # Already warm from startup - skip warmup stage entirely
                logger.info(f"[VBI-ORCH] {trace_id} | ECAPA already warm from startup, skipping warmup stage")

            # Check if we have audio data
            if not audio_data:
                await send_progress("audio_receive", "failed", error="No audio data")
                async with self.tracer.trace_step(trace_id, VBIStage.AUDIO_RECEIVE) as step:
                    step.details["has_audio"] = False
                    logger.warning(f"[VBI-ORCH] {trace_id} | No audio data provided")

                result["response"] = "No voice audio received. Please speak your command."
                self.tracer.complete_trace(trace_id, VBIStatus.FAILED)
                return result

            # Stage 1: Audio Receive
            await send_progress("audio_receive", "in_progress", {"audio_size": audio_size})
            async with self.tracer.trace_step(trace_id, VBIStage.AUDIO_RECEIVE) as step:
                step.details["has_audio"] = True
                step.details["audio_size_bytes"] = audio_size
                step.details["sample_rate"] = sample_rate
                step.details["mime_type"] = mime_type
            await send_progress("audio_receive", "success", {
                "audio_size": audio_size,
                "sample_rate": sample_rate,
                "format": mime_type
            })

            # Stage 2: Audio Decode
            await send_progress("audio_decode", "in_progress", {"message": "Decoding audio stream..."})
            async with self.tracer.trace_step(trace_id, VBIStage.AUDIO_DECODE) as step:
                decoded_audio = await asyncio.wait_for(
                    self._decode_audio(audio_data, mime_type),
                    timeout=self.timeouts["audio_processing"]
                )
                step.details["decoded_size"] = len(decoded_audio) if decoded_audio else 0
            await send_progress("audio_decode", "success", {
                "decoded_size": len(decoded_audio) if decoded_audio else 0
            })

            # Stage 3: Audio Preprocess
            await send_progress("audio_preprocess", "in_progress", {"message": "Enhancing audio quality..."})
            async with self.tracer.trace_step(trace_id, VBIStage.AUDIO_PREPROCESS) as step:
                processed_audio = await asyncio.wait_for(
                    self._preprocess_audio(decoded_audio, sample_rate),
                    timeout=self.timeouts["audio_processing"]
                )
                step.details["processed_samples"] = len(processed_audio) if processed_audio else 0
            await send_progress("audio_preprocess", "success", {
                "samples": len(processed_audio) if processed_audio else 0
            })

            # Stage 4: ECAPA Embedding Extraction (most important visual stage)
            await send_progress("ecapa_extract", "in_progress", {
                "message": "Extracting unique voiceprint...",
                "model": "ECAPA-TDNN"
            })
            async with self.tracer.trace_step(trace_id, VBIStage.ECAPA_EXTRACT) as step:
                embedding = await asyncio.wait_for(
                    self._extract_embedding(processed_audio),
                    timeout=self.timeouts["ecapa_extraction"]
                )
                step.details["embedding_size"] = len(embedding) if embedding else 0
            await send_progress("ecapa_extract", "success", {
                "embedding_dimensions": len(embedding) if embedding else 0,
                "model": "ECAPA-TDNN"
            })

            # Stage 5: Speaker Verification
            await send_progress("speaker_verify", "in_progress", {"message": "Comparing voiceprint to enrolled speakers..."})
            async with self.tracer.trace_step(trace_id, VBIStage.SPEAKER_VERIFY) as step:
                verification = await asyncio.wait_for(
                    self._verify_speaker(embedding),
                    timeout=self.timeouts["speaker_verification"]
                )
                step.details["speaker_name"] = verification.get("speaker_name", "Unknown")
                step.details["confidence"] = verification.get("confidence", 0.0)
                step.details["is_verified"] = verification.get("is_verified", False)
            await send_progress("speaker_verify", "success", {
                "speaker": verification.get("speaker_name", "Unknown"),
                "confidence": verification.get("confidence", 0.0),
                "verified": verification.get("is_verified", False)
            })

            # Stage 6: Decision
            await send_progress("decision", "in_progress", {"message": "Analyzing verification results..."})
            async with self.tracer.trace_step(trace_id, VBIStage.DECISION) as step:
                is_authorized = verification.get("is_verified", False)
                confidence = verification.get("confidence", 0.0)
                speaker_name = verification.get("speaker_name", "Unknown")

                step.details["is_authorized"] = is_authorized
                step.details["decision"] = "ALLOW" if is_authorized else "DENY"

            if not is_authorized:
                await send_progress("decision", "failed", {
                    "decision": "DENY",
                    "confidence": confidence,
                    "speaker": speaker_name
                }, error=f"Voice not verified (confidence: {confidence:.1%})")
                result["response"] = f"Voice verification failed. Confidence: {confidence:.1%}"
                result["confidence"] = confidence
                self.tracer.complete_trace(trace_id, VBIStatus.FAILED, confidence, speaker_name)
                return result

            await send_progress("decision", "success", {
                "decision": "ALLOW",
                "confidence": confidence,
                "speaker": speaker_name
            })

            # Stage 7: Unlock Execution
            await send_progress("unlock_execute", "in_progress", {
                "message": f"Unlocking screen for {speaker_name}...",
                "speaker": speaker_name
            })
            async with self.tracer.trace_step(trace_id, VBIStage.UNLOCK_EXECUTE) as step:
                unlock_result = await asyncio.wait_for(
                    self._execute_unlock(speaker_name),
                    timeout=self.timeouts["unlock_execution"]
                )
                step.details["unlock_success"] = unlock_result.get("success", False)
                step.details["method"] = unlock_result.get("method", "unknown")
            await send_progress("unlock_execute", "success", {
                "unlocked": unlock_result.get("success", False),
                "method": unlock_result.get("method", "unknown")
            })

            # Stage 8: Response Build (Complete!)
            async with self.tracer.trace_step(trace_id, VBIStage.RESPONSE_BUILD) as step:
                response_text = f"Verified. Unlocking for you, {speaker_name}."
                result["response"] = response_text
                result["success"] = True
                result["speaker_name"] = speaker_name
                result["confidence"] = confidence
                step.details["response_length"] = len(response_text)

            # Send completion progress
            await send_progress("complete", "success", {
                "speaker": speaker_name,
                "confidence": confidence,
                "message": response_text
            })

            # Complete trace
            self.tracer.complete_trace(trace_id, VBIStatus.SUCCESS, confidence, speaker_name)

            return result

        except asyncio.TimeoutError as e:
            result["response"] = "Voice verification timed out. Please try again."
            self.tracer.complete_trace(trace_id, VBIStatus.TIMEOUT)
            return result

        except Exception as e:
            logger.error(f"[VBI-ORCH] {trace_id} | Pipeline error: {e}")
            logger.error(traceback.format_exc())
            result["response"] = f"Voice verification error: {str(e)}"
            self.tracer.complete_trace(trace_id, VBIStatus.FAILED)
            return result

    async def _decode_audio(self, audio_data: str, mime_type: str) -> bytes:
        """Decode base64 audio data"""
        import base64

        try:
            return base64.b64decode(audio_data)
        except Exception as e:
            logger.error(f"[VBI-ORCH] Failed to decode audio: {e}")
            raise

    async def _preprocess_audio(self, audio_bytes: bytes, sample_rate: int) -> Any:
        """
        Preprocess audio for embedding extraction.
        
        Converts any audio format (WebM, OGG, MP3, etc.) to 16kHz mono WAV
        suitable for ECAPA-TDNN speaker verification.
        
        Args:
            audio_bytes: Raw audio bytes (any format)
            sample_rate: Original sample rate hint (may not be accurate for compressed formats)
            
        Returns:
            WAV file bytes ready for ECAPA embedding extraction
        """
        try:
            from voice.audio_format_converter import AudioFormatConverter, get_audio_converter
            
            # Use singleton converter for caching benefits
            converter = get_audio_converter()
            
            # Use the async method to avoid blocking the event loop
            # This handles all format detection and transcoding
            processed = await converter.convert_to_wav_async(
                audio_data=audio_bytes,
                target_sample_rate=16000,  # ECAPA requires 16kHz
                target_channels=1,         # Mono
                target_bit_depth=16        # 16-bit
            )
            
            if not processed or len(processed) < 44:  # 44 bytes is minimum WAV header
                logger.warning("[VBI-ORCH] Audio preprocessing returned empty/invalid WAV")
                raise ValueError("Audio preprocessing failed - empty result")
                
            logger.info(f"[VBI-ORCH] Audio preprocessed: {len(audio_bytes)} bytes → {len(processed)} bytes WAV")
            return processed

        except ImportError as e:
            # Fallback: try to use raw bytes (may not work for compressed formats)
            logger.warning(f"[VBI-ORCH] AudioFormatConverter not available: {e}, using raw audio")
            return audio_bytes
        except Exception as e:
            logger.error(f"[VBI-ORCH] Audio preprocessing failed: {type(e).__name__}: {e}")
            # Re-raise to trigger proper error handling in the pipeline
            raise RuntimeError(f"Audio preprocessing failed: {e}") from e

    async def _extract_embedding(self, audio_data: Any) -> List[float]:
        """
        Extract ECAPA-TDNN speaker embedding from audio with robust fallback chain.
        
        Fallback chain (v2.0):
        1. CloudECAPAClient (Cloud Run / Spot VM) - fastest, zero local memory
        2. ML Engine Registry (may use cloud or local) - orchestrated fallback
        3. Local ECAPA via SpeechBrain - last resort, high memory usage
        
        Args:
            audio_data: Preprocessed audio bytes (float32 or int16)
            
        Returns:
            192-dimensional speaker embedding as list of floats
        """
        import numpy as np
        
        # Convert audio_data to bytes if it's not already
        if hasattr(audio_data, 'tobytes'):
            audio_bytes = audio_data.tobytes()
        elif isinstance(audio_data, bytes):
            audio_bytes = audio_data
        else:
            audio_bytes = np.array(audio_data, dtype=np.float32).tobytes()
        
        last_error = None
        
        # =========================================================================
        # STRATEGY 1: Cloud ECAPA Client (fastest, no local memory)
        # =========================================================================
        try:
            logger.info("[VBI-ORCH] 🌐 Trying Cloud ECAPA extraction...")
            from voice_unlock.cloud_ecapa_client import get_cloud_ecapa_client
            
            client = await get_cloud_ecapa_client()
            
            if client is not None:
                # Extract with timeout
                embedding = await asyncio.wait_for(
                    client.extract_embedding(
                        audio_data=audio_bytes,
                        sample_rate=16000,
                        format="float32",
                        use_cache=True,
                        use_fast_path=True
                    ),
                    timeout=20.0  # 20 second timeout for cloud
                )
                
                if embedding is not None:
                    logger.info(f"[VBI-ORCH] ✅ Cloud ECAPA extraction succeeded: {len(embedding)} dimensions")
                    return embedding.tolist() if hasattr(embedding, 'tolist') else list(embedding)
                else:
                    last_error = "Cloud returned None"
                    logger.warning(f"[VBI-ORCH] ⚠️ Cloud ECAPA returned None, trying fallback...")
            else:
                last_error = "CloudECAPAClient not initialized"
                logger.warning("[VBI-ORCH] ⚠️ CloudECAPAClient not available, trying fallback...")
                
        except asyncio.TimeoutError:
            last_error = "Cloud ECAPA timeout (20s)"
            logger.warning("[VBI-ORCH] ⏱️ Cloud ECAPA timed out, trying fallback...")
        except ImportError as e:
            last_error = f"Import error: {e}"
            logger.warning(f"[VBI-ORCH] ⚠️ CloudECAPAClient import failed: {e}")
        except Exception as e:
            last_error = str(e)
            logger.warning(f"[VBI-ORCH] ⚠️ Cloud ECAPA failed: {type(e).__name__}: {e}")
        
        # =========================================================================
        # STRATEGY 2: ML Engine Registry (orchestrated cloud/local routing)
        # =========================================================================
        try:
            logger.info("[VBI-ORCH] 🔄 Trying ML Engine Registry extraction...")
            from voice_unlock.ml_engine_registry import extract_speaker_embedding, ensure_ecapa_available
            
            # Ensure ECAPA is available (may trigger cloud or local load)
            success, msg, _ = await ensure_ecapa_available()
            
            if success:
                # Convert bytes to numpy array for registry
                audio_array = np.frombuffer(audio_bytes, dtype=np.float32)
                
                embedding = await asyncio.wait_for(
                    extract_speaker_embedding(audio_array),
                    timeout=30.0  # 30 second timeout for registry
                )
                
                if embedding is not None:
                    embedding_list = embedding.tolist() if hasattr(embedding, 'tolist') else list(embedding)
                    logger.info(f"[VBI-ORCH] ✅ ML Registry extraction succeeded: {len(embedding_list)} dimensions")
                    return embedding_list
                else:
                    last_error = "Registry returned None"
                    logger.warning("[VBI-ORCH] ⚠️ ML Registry returned None, trying local...")
            else:
                last_error = msg
                logger.warning(f"[VBI-ORCH] ⚠️ ML Registry not available: {msg}")
                
        except asyncio.TimeoutError:
            last_error = "ML Registry timeout (30s)"
            logger.warning("[VBI-ORCH] ⏱️ ML Registry timed out, trying local...")
        except ImportError as e:
            last_error = f"Import error: {e}"
            logger.warning(f"[VBI-ORCH] ⚠️ ML Registry import failed: {e}")
        except Exception as e:
            last_error = str(e)
            logger.warning(f"[VBI-ORCH] ⚠️ ML Registry failed: {type(e).__name__}: {e}")
        
        # =========================================================================
        # STRATEGY 3: Direct Local SpeechBrain (last resort - high memory)
        # =========================================================================
        try:
            logger.info("[VBI-ORCH] 💻 Trying direct local SpeechBrain extraction (last resort)...")
            from voice.speaker_verification_service import get_speaker_verification_service
            
            service = get_speaker_verification_service()
            if service is None:
                service = await get_speaker_verification_service()
            
            if service and hasattr(service, '_local_engine') and service._local_engine:
                audio_array = np.frombuffer(audio_bytes, dtype=np.float32)
                
                embedding = await asyncio.wait_for(
                    service._local_engine.encode_batch(audio_array),
                    timeout=45.0  # 45 second timeout for local
                )
                
                if embedding is not None:
                    # Handle batch output shape
                    if len(embedding.shape) > 1:
                        embedding = embedding.squeeze()
                    embedding_list = embedding.tolist() if hasattr(embedding, 'tolist') else list(embedding)
                    logger.info(f"[VBI-ORCH] ✅ Local SpeechBrain extraction succeeded: {len(embedding_list)} dimensions")
                    return embedding_list
                    
        except asyncio.TimeoutError:
            last_error = "Local SpeechBrain timeout (45s)"
            logger.warning("[VBI-ORCH] ⏱️ Local SpeechBrain timed out")
        except ImportError as e:
            last_error = f"Import error: {e}"
            logger.warning(f"[VBI-ORCH] ⚠️ Local SpeechBrain import failed: {e}")
        except Exception as e:
            last_error = str(e)
            logger.warning(f"[VBI-ORCH] ⚠️ Local SpeechBrain failed: {type(e).__name__}: {e}")
        
        # =========================================================================
        # ALL STRATEGIES FAILED
        # =========================================================================
        error_msg = f"All ECAPA extraction strategies failed. Last error: {last_error}"
        logger.error(f"[VBI-ORCH] ❌ {error_msg}")
        raise RuntimeError(error_msg)

    async def _verify_speaker(self, embedding: List[float]) -> Dict[str, Any]:
        """
        Verify speaker embedding against enrolled profiles.
        
        Uses multiple verification strategies:
        1. Unified voice cache (fast-path for recent speakers)
        2. Learning database profiles (cosine similarity comparison)
        3. Voice Biometric Intelligence (advanced multi-modal)
        
        Args:
            embedding: 192-dimensional ECAPA-TDNN speaker embedding
            
        Returns:
            Dict with is_verified, speaker_name, confidence
        """
        import numpy as np
        
        try:
            test_embedding = np.array(embedding, dtype=np.float32)
            
            # Normalize the embedding for cosine similarity
            test_norm = test_embedding / (np.linalg.norm(test_embedding) + 1e-10)
            
            best_match = {
                "is_verified": False,
                "speaker_name": "Unknown",
                "confidence": 0.0
            }
            
            # Strategy 1: Try unified voice cache first (fastest)
            try:
                from voice_unlock.unified_voice_cache_manager import get_unified_voice_cache
                unified_cache = await get_unified_voice_cache()
                
                if unified_cache and unified_cache.is_ready:
                    preloaded = unified_cache.get_preloaded_profiles()
                    logger.info(f"[VBI-ORCH] ⚡ Unified cache has {len(preloaded)} preloaded profiles")
                    
                    for profile_name, profile in preloaded.items():
                        if profile.embedding is not None:
                            profile_embedding = np.array(profile.embedding, dtype=np.float32)
                            profile_norm = profile_embedding / (np.linalg.norm(profile_embedding) + 1e-10)
                            
                            # Cosine similarity
                            similarity = float(np.dot(test_norm, profile_norm))
                            logger.info(f"[VBI-ORCH] ⚡ Cache profile '{profile_name}': similarity={similarity:.4f}")
                            
                            if similarity > best_match["confidence"]:
                                best_match = {
                                    "is_verified": similarity >= 0.40,  # Unlock threshold
                                    "speaker_name": profile_name,
                                    "confidence": similarity
                                }
                        else:
                            logger.debug(f"[VBI-ORCH] ⚡ Cache profile '{profile_name}' has no embedding")
                                
                    if best_match["is_verified"]:
                        logger.info(f"[VBI-ORCH] ⚡ Unified cache match: {best_match['speaker_name']} ({best_match['confidence']:.1%})")
                        return best_match
                else:
                    logger.warning(f"[VBI-ORCH] ⚠️ Unified cache not ready: cache={unified_cache is not None}, ready={unified_cache.is_ready if unified_cache else 'N/A'}")
                        
            except Exception as e:
                logger.warning(f"[VBI-ORCH] Unified cache check failed: {e}")
            
            # Strategy 2: Check learning database profiles
            try:
                from intelligence.learning_database import get_learning_database
                db = await get_learning_database()
                
                if db:
                    profiles = await db.get_all_speaker_profiles()
                    logger.info(f"[VBI-ORCH] 📊 Learning DB returned {len(profiles)} profiles")
                    
                    for profile in profiles:
                        # CRITICAL FIX: Database returns 'voiceprint_embedding', not 'embedding'
                        profile_embedding = profile.get("voiceprint_embedding") or profile.get("embedding")
                        speaker_name = profile.get("speaker_name", profile.get("name", "Unknown"))
                        
                        if profile_embedding is None:
                            logger.debug(f"[VBI-ORCH] Profile '{speaker_name}' has no embedding")
                            continue
                            
                        # Handle bytes vs numpy array
                        if isinstance(profile_embedding, bytes):
                            profile_array = np.frombuffer(profile_embedding, dtype=np.float32)
                        elif isinstance(profile_embedding, (list, tuple)):
                            profile_array = np.array(profile_embedding, dtype=np.float32)
                        else:
                            profile_array = np.array(profile_embedding, dtype=np.float32)
                        
                        if len(profile_array) < 50:
                            logger.debug(f"[VBI-ORCH] Profile '{speaker_name}' embedding too short: {len(profile_array)}")
                            continue
                            
                        profile_norm = profile_array / (np.linalg.norm(profile_array) + 1e-10)
                        
                        # Cosine similarity
                        similarity = float(np.dot(test_norm, profile_norm))
                        logger.info(f"[VBI-ORCH] 📊 Profile '{speaker_name}': similarity={similarity:.4f} (dim={len(profile_array)})")
                        
                        if similarity > best_match["confidence"]:
                            best_match = {
                                "is_verified": similarity >= 0.40,
                                "speaker_name": speaker_name,
                                "confidence": similarity
                            }
                    
                    if best_match["is_verified"]:
                        logger.info(f"[VBI-ORCH] 🔐 Database match: {best_match['speaker_name']} ({best_match['confidence']:.1%})")
                        return best_match
                        
            except Exception as e:
                logger.warning(f"[VBI-ORCH] Learning database check failed: {e}")
            
            # Strategy 3: Try Voice Biometric Intelligence
            try:
                from voice_unlock.voice_biometric_intelligence import get_voice_biometric_intelligence
                vbi = await get_voice_biometric_intelligence()
                
                if vbi and vbi._unified_cache:
                    for profile_name, profile in vbi._unified_cache.get_preloaded_profiles().items():
                        if profile.embedding is not None:
                            profile_embedding = np.array(profile.embedding, dtype=np.float32)
                            profile_norm = profile_embedding / (np.linalg.norm(profile_embedding) + 1e-10)
                            
                            similarity = float(np.dot(test_norm, profile_norm))
                            
                            if similarity > best_match["confidence"]:
                                best_match = {
                                    "is_verified": similarity >= 0.40,
                                    "speaker_name": profile_name,
                                    "confidence": similarity
                                }
                                
                    if best_match["is_verified"]:
                        logger.info(f"[VBI-ORCH] 🧠 VBI match: {best_match['speaker_name']} ({best_match['confidence']:.1%})")
                        return best_match
                        
            except Exception as e:
                logger.debug(f"[VBI-ORCH] VBI check failed: {e}")
            
            # =================================================================
            # Strategy 4: Direct CloudSQL Query (CRITICAL - profiles stored here!)
            # This bypasses all caches and goes straight to the source
            # =================================================================
            logger.info("[VBI-ORCH] ☁️ Strategy 4: Direct CloudSQL query...")
            try:
                from intelligence.cloud_sql_connection_manager import get_connection_manager
                from intelligence.cloud_database_adapter import DatabaseConfig
                
                conn_manager = get_connection_manager()
                
                # Initialize CloudSQL connection if not already done
                if not conn_manager.is_initialized:
                    logger.info("[VBI-ORCH] ☁️ Initializing CloudSQL connection...")
                    config = DatabaseConfig()
                    
                    if config.use_cloud_sql:
                        success = await asyncio.wait_for(
                            conn_manager.initialize(
                                host=config.db_host,
                                port=config.db_port,
                                database=config.db_name,
                                user=config.db_user,
                                password=config.db_password,
                                max_connections=2
                            ),
                            timeout=10.0
                        )
                        if success:
                            logger.info("[VBI-ORCH] ✅ CloudSQL initialized")
                        else:
                            logger.warning("[VBI-ORCH] ⚠️ CloudSQL init failed")
                    else:
                        logger.warning("[VBI-ORCH] ⚠️ CloudSQL not configured")
                
                if conn_manager.is_initialized:
                    async with conn_manager.connection() as conn:
                        rows = await conn.fetch("""
                            SELECT speaker_name, voiceprint_embedding, embedding_dimension
                            FROM speaker_profiles
                            WHERE voiceprint_embedding IS NOT NULL
                        """)
                        
                        logger.info(f"[VBI-ORCH] ☁️ CloudSQL has {len(rows)} speaker profiles")
                        
                        for row in rows:
                            speaker_name = row['speaker_name']
                            embedding_blob = row['voiceprint_embedding']
                            
                            if embedding_blob:
                                # Convert bytes to numpy array
                                profile_embedding = np.frombuffer(embedding_blob, dtype=np.float32)
                                
                                if len(profile_embedding) >= 50:  # Valid embedding
                                    profile_norm = profile_embedding / (np.linalg.norm(profile_embedding) + 1e-10)
                                    
                                    # Cosine similarity
                                    similarity = float(np.dot(test_norm, profile_norm))
                                    logger.info(f"[VBI-ORCH] ☁️ CloudSQL '{speaker_name}': similarity={similarity:.4f}")
                                    
                                    if similarity > best_match["confidence"]:
                                        best_match = {
                                            "is_verified": similarity >= 0.40,
                                            "speaker_name": speaker_name,
                                            "confidence": similarity
                                        }
                        
                        if best_match["is_verified"]:
                            logger.info(f"[VBI-ORCH] ☁️ CloudSQL match: {best_match['speaker_name']} ({best_match['confidence']:.1%})")
                            return best_match
                            
            except asyncio.TimeoutError:
                logger.warning("[VBI-ORCH] ⚠️ CloudSQL connection timeout")
            except Exception as e:
                logger.warning(f"[VBI-ORCH] ⚠️ CloudSQL query failed: {type(e).__name__}: {e}")
            
            # =================================================================
            # Strategy 5: Direct SQLite Query (user said profiles are synced here)
            # =================================================================
            if best_match["confidence"] == 0:
                logger.info("[VBI-ORCH] 💾 Strategy 5: Direct SQLite query...")
                try:
                    import aiosqlite
                    from pathlib import Path
                    
                    # Try multiple SQLite paths
                    sqlite_paths = [
                        Path.home() / ".jarvis" / "learning" / "voice_biometrics_sync.db",
                        Path.home() / ".jarvis" / "jarvis_learning.db",
                        Path.home() / ".jarvis" / "learning" / "jarvis_learning.db",
                    ]
                    
                    for db_path in sqlite_paths:
                        if db_path.exists():
                            logger.info(f"[VBI-ORCH] 💾 Found SQLite: {db_path}")
                            async with aiosqlite.connect(str(db_path)) as conn:
                                conn.row_factory = aiosqlite.Row
                                cursor = await conn.execute("""
                                    SELECT speaker_name, voiceprint_embedding, total_samples
                                    FROM speaker_profiles
                                    WHERE voiceprint_embedding IS NOT NULL
                                """)
                                rows = await cursor.fetchall()
                                
                                logger.info(f"[VBI-ORCH] 💾 SQLite has {len(rows)} profiles with embeddings")
                                
                                for row in rows:
                                    speaker_name = row['speaker_name']
                                    embedding_blob = row['voiceprint_embedding']
                                    total_samples = row['total_samples']
                                    
                                    if embedding_blob:
                                        profile_embedding = np.frombuffer(embedding_blob, dtype=np.float32)
                                        
                                        if len(profile_embedding) >= 50:
                                            profile_norm = profile_embedding / (np.linalg.norm(profile_embedding) + 1e-10)
                                            similarity = float(np.dot(test_norm, profile_norm))
                                            
                                            logger.info(f"[VBI-ORCH] 💾 SQLite '{speaker_name}' (samples={total_samples}): similarity={similarity:.4f}")
                                            
                                            if similarity > best_match["confidence"]:
                                                best_match = {
                                                    "is_verified": similarity >= 0.40,
                                                    "speaker_name": speaker_name,
                                                    "confidence": similarity
                                                }
                                                
                                if best_match["is_verified"]:
                                    logger.info(f"[VBI-ORCH] 💾 SQLite match: {best_match['speaker_name']} ({best_match['confidence']:.1%})")
                                    return best_match
                            break  # Found a working SQLite database
                            
                except Exception as e:
                    logger.warning(f"[VBI-ORCH] ⚠️ SQLite query failed: {type(e).__name__}: {e}")
            
            # Log the result even if not verified
            if best_match["confidence"] > 0:
                logger.warning(
                    f"[VBI-ORCH] Best match below threshold: {best_match['speaker_name']} "
                    f"({best_match['confidence']:.1%} < 40%)"
                )
            else:
                logger.error("[VBI-ORCH] ❌ No matching profiles found in ANY source!")
                logger.error("[VBI-ORCH] Check: 1) Profile enrolled 2) Embeddings synced 3) SQLite populated")
                
            return best_match

        except Exception as e:
            logger.error(f"[VBI-ORCH] Speaker verification failed: {type(e).__name__}: {e}")
            raise

    async def _execute_unlock(self, speaker_name: str) -> Dict[str, Any]:
        """Execute screen unlock"""
        try:
            from voice_unlock.macos_screen_lock import MacOSScreenLock
            lock = MacOSScreenLock()

            result = await asyncio.to_thread(lock.unlock, speaker_name)

            return {
                "success": result,
                "method": "keychain",
                "speaker": speaker_name
            }

        except Exception as e:
            logger.error(f"[VBI-ORCH] Unlock execution failed: {e}")
            raise

    def get_diagnostics(self) -> Dict[str, Any]:
        """Get pipeline diagnostics"""
        return {
            "tracer_stats": self.tracer.get_performance_stats(),
            "prewarmer_status": self.prewarmer.get_status(),
            "timeouts": self.timeouts,
            "recent_traces": self.tracer.get_recent_traces(5)
        }


# Singleton instances
_tracer: Optional[VBIDebugTracer] = None
_prewarmer: Optional[ECAPAPreWarmer] = None
_orchestrator: Optional[VBIPipelineOrchestrator] = None


def get_tracer() -> VBIDebugTracer:
    """Get the singleton VBI debug tracer"""
    global _tracer
    if _tracer is None:
        _tracer = VBIDebugTracer()
    return _tracer


def get_prewarmer() -> ECAPAPreWarmer:
    """Get the singleton ECAPA pre-warmer"""
    global _prewarmer
    if _prewarmer is None:
        _prewarmer = ECAPAPreWarmer()
    return _prewarmer


def get_orchestrator() -> VBIPipelineOrchestrator:
    """Get the singleton VBI pipeline orchestrator"""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = VBIPipelineOrchestrator()
    return _orchestrator


async def prewarm_vbi_at_startup():
    """
    Call this at startup (in start_system.py) to ensure ECAPA is warm

    This eliminates cold starts during actual unlock requests.
    """
    logger.info("=" * 70)
    logger.info("VBI STARTUP PRE-WARM")
    logger.info("=" * 70)

    prewarmer = get_prewarmer()
    result = await prewarmer.warmup(force=True)

    if result.get("status") == "success":
        logger.info(f"[VBI-STARTUP]  ECAPA pre-warmed in {result.get('total_duration_ms', 0):.0f}ms")
        return True
    else:
        logger.error(f"[VBI-STARTUP]  Pre-warm failed: {result}")
        return False


# =============================================================================
# PARALLEL ORCHESTRATOR INTEGRATION
# =============================================================================

async def process_voice_unlock_parallel(
    command: str,
    audio_data: Optional[bytes] = None,
    sample_rate: int = 16000,
    progress_callback: Optional[Callable[[Dict[str, Any]], Any]] = None,
    use_parallel: bool = True,
) -> Dict[str, Any]:
    """
    Process voice unlock using the parallel VBI orchestrator.
    
    This is the recommended entry point for voice unlock operations.
    Uses the new ParallelVBIOrchestrator for faster, more robust verification.
    
    Args:
        command: Voice command text
        audio_data: Raw audio bytes (can be base64 encoded string)
        sample_rate: Audio sample rate (default 16000)
        progress_callback: Optional callback for progress updates
        use_parallel: Use parallel orchestrator (default True)
    
    Returns:
        Dict with verification result and pipeline metadata
    """
    if not audio_data:
        return {
            "success": False,
            "response": "No audio data provided",
            "error": "missing_audio",
        }
    
    # Convert base64 if needed
    if isinstance(audio_data, str):
        import base64
        audio_data = base64.b64decode(audio_data)
    
    if use_parallel:
        try:
            from core.vbi_parallel_integration import verify_voice_with_progress
            
            result = await verify_voice_with_progress(
                audio_data=audio_data,
                context={
                    "sample_rate": sample_rate,
                    "command": command,
                },
                speak=False,
                progress_callback=progress_callback,
            )
            
            return {
                "success": result.verified,
                "response": result.announcement,
                "speaker_name": result.speaker_name,
                "confidence": result.confidence,
                "verification_method": result.verification_method.value,
                "verification_time_ms": result.verification_time_ms,
                "stages_completed": result.stages_completed,
                "stages_failed": result.stages_failed,
                "stage_results": result.stage_results,
                "decision_factors": result.decision_factors,
                "warnings": result.warnings,
            }
            
        except ImportError as e:
            logger.warning(f"Parallel orchestrator not available: {e}, falling back to legacy")
        except Exception as e:
            logger.warning(f"Parallel orchestrator failed: {e}, falling back to legacy")
    
    # Fallback to legacy orchestrator
    orchestrator = get_orchestrator()
    
    # Encode audio for legacy orchestrator
    import base64
    audio_b64 = base64.b64encode(audio_data).decode('utf-8')
    
    return await orchestrator.process_voice_unlock(
        command=command,
        audio_data=audio_b64,
        sample_rate=sample_rate,
        progress_callback=progress_callback,
    )


async def get_vbi_diagnostics() -> Dict[str, Any]:
    """
    Get comprehensive VBI diagnostics from all subsystems.
    
    Returns:
        Dict with health status, stats, and configuration from:
        - Legacy VBI Pipeline Orchestrator
        - Parallel VBI Orchestrator
        - ECAPA Pre-warmer
        - Debug Tracer
    """
    diagnostics = {
        "timestamp": datetime.now().isoformat(),
        "components": {},
    }
    
    # Legacy orchestrator
    try:
        orchestrator = get_orchestrator()
        diagnostics["components"]["legacy_orchestrator"] = orchestrator.get_diagnostics()
    except Exception as e:
        diagnostics["components"]["legacy_orchestrator"] = {"error": str(e)}
    
    # Parallel orchestrator
    try:
        from core.parallel_vbi_orchestrator import get_parallel_vbi_orchestrator
        parallel = await get_parallel_vbi_orchestrator()
        diagnostics["components"]["parallel_orchestrator"] = {
            "health": parallel.get_health(),
            "stats": parallel.get_stats(),
        }
    except Exception as e:
        diagnostics["components"]["parallel_orchestrator"] = {"error": str(e)}
    
    # Pre-warmer
    try:
        prewarmer = get_prewarmer()
        diagnostics["components"]["ecapa_prewarmer"] = prewarmer.get_status()
    except Exception as e:
        diagnostics["components"]["ecapa_prewarmer"] = {"error": str(e)}
    
    # Debug tracer
    try:
        tracer = get_tracer()
        diagnostics["components"]["debug_tracer"] = tracer.get_performance_stats()
    except Exception as e:
        diagnostics["components"]["debug_tracer"] = {"error": str(e)}
    
    # VBI Health Monitor
    try:
        from core.vbi_health_monitor import get_vbi_health_monitor
        monitor = await get_vbi_health_monitor()
        diagnostics["components"]["health_monitor"] = await monitor.get_system_health()
    except Exception as e:
        diagnostics["components"]["health_monitor"] = {"error": str(e)}
    
    # Overall health determination
    overall_health = "healthy"
    for component, data in diagnostics["components"].items():
        if isinstance(data, dict):
            if data.get("error"):
                overall_health = "degraded"
            elif data.get("health", {}).get("status") == "critical":
                overall_health = "critical"
                break
            elif data.get("health", {}).get("status") == "degraded":
                overall_health = "degraded"
    
    diagnostics["overall_health"] = overall_health
    
    return diagnostics
