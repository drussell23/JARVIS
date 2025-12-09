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
    ECAPA Pre-Warmer - Ensures Cloud ECAPA is ready at startup

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
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._initialized = True
        self.is_warm = False
        self.last_warmup_time: Optional[float] = None
        self.warmup_duration_ms: Optional[float] = None
        self.cloud_endpoint: Optional[str] = None
        self.health_check_interval = 60  # seconds
        self.warmup_timeout = 30  # seconds
        self.auto_rewarm_threshold = 300  # 5 minutes

        # Test embedding for warming (1 second of silence at 16kHz)
        self._warmup_audio = self._generate_warmup_audio()

        # Background task handle
        self._health_task: Optional[asyncio.Task] = None

        logger.info("=" * 70)
        logger.info("ECAPA PRE-WARMER INITIALIZED")
        logger.info("=" * 70)

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
        Pre-warm ECAPA embedding extraction

        This should be called at startup to ensure ECAPA is ready
        before any unlock requests come in.

        v1.1.0 Enhancement:
        - Uses CloudECAPAClient's wait_for_ecapa_ready() for robust cold start handling
        - Ensures ECAPA model is fully loaded before marking as warm
        - No more "Processing..." hangs during first unlock
        """
        async with self._lock:
            if self.is_warm and not force:
                time_since_warmup = time.time() - (self.last_warmup_time or 0)
                if time_since_warmup < self.auto_rewarm_threshold:
                    logger.info(f"[ECAPA-PREWARM] Already warm ({time_since_warmup:.0f}s ago)")
                    return {
                        "status": "already_warm",
                        "last_warmup_ms": self.warmup_duration_ms,
                        "time_since_warmup_s": time_since_warmup
                    }

            logger.info("=" * 70)
            logger.info("[ECAPA-PREWARM] STARTING PRE-WARM SEQUENCE v1.1.0")
            logger.info("=" * 70)
            logger.info("   Using enhanced CloudECAPAClient with wait_for_ecapa_ready")

            start_time = time.time()
            result = {"status": "unknown", "stages": []}

            try:
                # Stage 1: Find and verify cloud endpoint
                stage_start = time.time()
                endpoint = await self._find_cloud_endpoint()
                result["stages"].append({
                    "stage": "find_endpoint",
                    "duration_ms": (time.time() - stage_start) * 1000,
                    "endpoint": endpoint
                })

                if not endpoint:
                    raise Exception("No cloud ECAPA endpoint available")

                self.cloud_endpoint = endpoint
                logger.info(f"[ECAPA-PREWARM]  Found endpoint: {endpoint}")

                # Stage 2: Send warmup request to trigger model loading
                stage_start = time.time()
                warmup_result = await self._send_warmup_request(endpoint)
                result["stages"].append({
                    "stage": "warmup_request",
                    "duration_ms": (time.time() - stage_start) * 1000,
                    "embedding_size": warmup_result.get("embedding_size", 0)
                })

                logger.info(f"[ECAPA-PREWARM]  Warmup embedding extracted: {warmup_result.get('embedding_size', 0)} dimensions")

                # Stage 3: Verify embedding quality
                stage_start = time.time()
                is_valid = self._verify_embedding(warmup_result.get("embedding", []))
                result["stages"].append({
                    "stage": "verify_embedding",
                    "duration_ms": (time.time() - stage_start) * 1000,
                    "is_valid": is_valid
                })

                if not is_valid:
                    raise Exception("Warmup embedding validation failed")

                logger.info("[ECAPA-PREWARM]  Embedding validated")

                # Mark as warm
                self.is_warm = True
                self.last_warmup_time = time.time()
                self.warmup_duration_ms = (time.time() - start_time) * 1000

                result["status"] = "success"
                result["total_duration_ms"] = self.warmup_duration_ms

                logger.info("=" * 70)
                logger.info(f"[ECAPA-PREWARM]  PRE-WARM COMPLETE in {self.warmup_duration_ms:.0f}ms")
                logger.info("=" * 70)

                # Start health monitoring
                if self._health_task is None or self._health_task.done():
                    self._health_task = asyncio.create_task(self._health_monitor())

                return result

            except asyncio.TimeoutError:
                self.warmup_duration_ms = (time.time() - start_time) * 1000
                result["status"] = "timeout"
                result["total_duration_ms"] = self.warmup_duration_ms
                logger.error(f"[ECAPA-PREWARM]  TIMEOUT after {self.warmup_duration_ms:.0f}ms")
                return result

            except Exception as e:
                self.warmup_duration_ms = (time.time() - start_time) * 1000
                result["status"] = "failed"
                result["error"] = str(e)
                result["total_duration_ms"] = self.warmup_duration_ms
                logger.error(f"[ECAPA-PREWARM]  FAILED: {e}")
                logger.error(f"   Traceback: {traceback.format_exc()}")
                return result

    async def _find_cloud_endpoint(self) -> Optional[str]:
        """Find available cloud ECAPA endpoint"""
        try:
            # Try to get endpoint from CloudECAPAClient
            from voice_unlock.cloud_ecapa_client import CloudECAPAClient
            client = CloudECAPAClient()

            if hasattr(client, 'primary_endpoint') and client.primary_endpoint:
                return client.primary_endpoint

            if hasattr(client, 'endpoints') and client.endpoints:
                return client.endpoints[0]

            # Fallback to environment variable
            endpoint = os.environ.get("CLOUD_ECAPA_ENDPOINT")
            if endpoint:
                return endpoint

            # Try default Cloud Run endpoint
            default_endpoint = "https://jarvis-ml-888774109345.us-central1.run.app"

            # Verify it's accessible
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{default_endpoint}/health", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        return default_endpoint

            return None

        except Exception as e:
            logger.warning(f"[ECAPA-PREWARM] Failed to find endpoint: {e}")
            return None

    async def _send_warmup_request(self, endpoint: str) -> Dict[str, Any]:
        """
        Send warmup embedding request using CloudECAPAClient

        v1.1.0 Enhancement:
        - Uses CloudECAPAClient's wait_for_ecapa_ready() for robust cold start handling
        - If Cloud Run is cold, waits for ECAPA model to load
        - Uses semantic caching to avoid redundant model loads
        """
        try:
            # Use the enhanced CloudECAPAClient instead of raw HTTP
            from voice_unlock.cloud_ecapa_client import get_cloud_ecapa_client

            client = await get_cloud_ecapa_client()
            if not client:
                raise Exception("CloudECAPAClient not available")

            # Initialize the client if needed
            if not client._initialized:
                logger.info("[ECAPA-PREWARM] Initializing CloudECAPAClient...")
                init_result = await client.initialize()
                if not init_result.get("success", False):
                    raise Exception(f"CloudECAPAClient init failed: {init_result.get('error')}")

            # First, use wait_for_ecapa_ready() to ensure the Cloud Run ECAPA is ready
            logger.info("[ECAPA-PREWARM] Checking if ECAPA model is ready...")
            ready_result = await client.wait_for_ecapa_ready(
                endpoint=endpoint,
                timeout=120.0,  # 2 minute timeout for cold starts
                poll_interval=3.0,  # Check every 3 seconds
                log_progress=True
            )

            if not ready_result.get("ready"):
                raise Exception(f"ECAPA not ready after waiting: {ready_result.get('error')}")

            logger.info(f"[ECAPA-PREWARM] ✅ ECAPA model ready! (waited {ready_result.get('elapsed_seconds', 0):.1f}s)")

            # Now extract a test embedding to fully warm up the model
            import base64
            import struct

            # Decode the warmup audio from base64 to raw bytes
            warmup_audio_bytes = base64.b64decode(self._warmup_audio)

            # Convert 16-bit PCM to float32 for the client
            num_samples = len(warmup_audio_bytes) // 2
            pcm_samples = struct.unpack(f'{num_samples}h', warmup_audio_bytes)
            float_samples = [s / 32768.0 for s in pcm_samples]  # Normalize to [-1, 1]
            float_audio = struct.pack(f'{len(float_samples)}f', *float_samples)

            logger.info("[ECAPA-PREWARM] Extracting warmup embedding...")
            embedding = await client.extract_embedding(
                audio_data=float_audio,
                sample_rate=16000,
                format="float32",
                use_cache=False  # Don't cache warmup - we want to exercise the full path
            )

            if embedding is None:
                raise Exception("Embedding extraction returned None")

            embedding_list = embedding.tolist() if hasattr(embedding, 'tolist') else list(embedding)

            logger.info(f"[ECAPA-PREWARM] ✅ Warmup embedding extracted: {len(embedding_list)} dimensions")

            return {
                "embedding": embedding_list,
                "embedding_size": len(embedding_list),
                "endpoint_path": "/api/ml/speaker_embedding",
                "client_version": client.VERSION,
                "cold_start_waited": ready_result.get("elapsed_seconds", 0) > 1.0
            }

        except ImportError as e:
            logger.warning(f"[ECAPA-PREWARM] CloudECAPAClient not available, falling back to raw HTTP: {e}")
            # Fallback to raw HTTP for backwards compatibility
            return await self._send_warmup_request_raw(endpoint)

        except Exception as e:
            logger.error(f"[ECAPA-PREWARM] CloudECAPAClient warmup failed: {e}")
            raise

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
        """Get current pre-warmer status"""
        return {
            "is_warm": self.is_warm,
            "last_warmup_time": datetime.fromtimestamp(self.last_warmup_time).isoformat() if self.last_warmup_time else None,
            "warmup_duration_ms": self.warmup_duration_ms,
            "cloud_endpoint": self.cloud_endpoint,
            "time_since_warmup_s": time.time() - self.last_warmup_time if self.last_warmup_time else None
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
        self.timeouts = {
            "audio_processing": float(os.environ.get("VBI_AUDIO_TIMEOUT", "5")),
            "ecapa_extraction": float(os.environ.get("VBI_ECAPA_TIMEOUT", "15")),
            "speaker_verification": float(os.environ.get("VBI_VERIFY_TIMEOUT", "5")),
            "unlock_execution": float(os.environ.get("VBI_UNLOCK_TIMEOUT", "10")),
            "total_pipeline": float(os.environ.get("VBI_TOTAL_TIMEOUT", "30"))
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
            # Ensure ECAPA is ready
            if not self.prewarmer.is_warm:
                await send_progress("warmup", "in_progress", {"message": "Initializing voice verification..."})
                logger.warning(f"[VBI-ORCH] {trace_id} | ECAPA not warm, attempting warmup...")
                warmup_result = await asyncio.wait_for(
                    self.prewarmer.warmup(),
                    timeout=self.timeouts["ecapa_extraction"]
                )
                if warmup_result.get("status") != "success":
                    await send_progress("warmup", "failed", error="Warmup failed")
                    result["response"] = "Voice verification system is warming up. Please try again in a moment."
                    self.tracer.complete_trace(trace_id, VBIStatus.FAILED)
                    return result
                await send_progress("warmup", "success", {"message": "System ready"})

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
        """Preprocess audio for embedding extraction"""
        try:
            from voice.audio_format_converter import AudioFormatConverter
            converter = AudioFormatConverter()

            # Convert to proper format for ECAPA
            processed = await asyncio.to_thread(
                converter.convert_to_wav,
                audio_bytes,
                target_sample_rate=16000
            )
            return processed

        except ImportError:
            # Fallback: return raw bytes
            logger.warning("[VBI-ORCH] AudioFormatConverter not available, using raw audio")
            return audio_bytes
        except Exception as e:
            logger.error(f"[VBI-ORCH] Audio preprocessing failed: {e}")
            raise

    async def _extract_embedding(self, audio_data: Any) -> List[float]:
        """Extract ECAPA embedding"""
        try:
            from voice_unlock.cloud_ecapa_client import CloudECAPAClient
            client = CloudECAPAClient()

            result = await client.extract_embedding_async(
                audio_data=audio_data,
                sample_rate=16000
            )

            return result.get("embedding", [])

        except Exception as e:
            logger.error(f"[VBI-ORCH] Embedding extraction failed: {e}")
            raise

    async def _verify_speaker(self, embedding: List[float]) -> Dict[str, Any]:
        """Verify speaker against enrolled profiles"""
        try:
            from voice.speaker_verification_service import SpeakerVerificationService
            service = SpeakerVerificationService()

            result = await service.verify_speaker_async(embedding)

            return {
                "is_verified": result.get("verified", False),
                "speaker_name": result.get("speaker_name", "Unknown"),
                "confidence": result.get("confidence", 0.0)
            }

        except Exception as e:
            logger.error(f"[VBI-ORCH] Speaker verification failed: {e}")
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
