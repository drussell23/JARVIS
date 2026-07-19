"""Quarantined supervisor zones — Migration Slice 1 (2026-07-19).
Source preserved byte-identical from unified_supervisor.py; any
dynamic consumer is revived through the PEP-562 symbol net with a
[QUARANTINE_BREACH] beacon. NEVER import this module directly.
"""
from __future__ import annotations  # noqa: F401

class DistributedObservabilitySystem:
    """
    Enterprise-grade observability for the Trinity system.

    Provides comprehensive monitoring, tracing, and alerting:
    - Distributed tracing with W3C Trace Context
    - Cross-repo metrics aggregation (Prometheus-compatible)
    - Centralized logging with structured JSON
    - Performance profiling and flame graphs
    - Error aggregation with deduplication
    - Health dashboard with unified view
    - Intelligent alerting with deduplication
    """

    def __init__(
        self,
        component_name: str = "unified_kernel",
        metrics_port: int = 9090,
        enable_tracing: bool = True,
        enable_profiling: bool = False,
        log_dir: Optional[Path] = None,
    ) -> None:
        self._component_name = component_name
        self._metrics_port = metrics_port
        self._enable_tracing = enable_tracing
        self._enable_profiling = enable_profiling
        self._log_dir = log_dir or Path.home() / ".jarvis" / "logs"
        self._log_dir.mkdir(parents=True, exist_ok=True)

        # Metrics storage
        self._counters: Dict[str, int] = {}
        self._gauges: Dict[str, float] = {}
        self._histograms: Dict[str, List[float]] = {}

        # Trace storage
        self._active_traces: Dict[str, Dict[str, Any]] = {}
        self._completed_traces: List[Dict[str, Any]] = []
        self._max_traces = 1000

        # Error aggregation
        self._error_counts: Dict[str, int] = {}
        self._recent_errors: List[Dict[str, Any]] = []
        self._max_errors = 100

        # Alerting
        self._alert_rules: List[Dict[str, Any]] = []
        self._fired_alerts: Dict[str, float] = {}  # alert_id -> last_fired_time
        self._alert_cooldown = 300.0  # 5 minutes

        # Background tasks
        self._metrics_server_task: Optional[asyncio.Task] = None
        self._running = False

        # Statistics
        self._stats = {
            "metrics_collected": 0,
            "traces_recorded": 0,
            "errors_aggregated": 0,
            "alerts_fired": 0,
        }

    async def start(self) -> bool:
        """Start the observability system."""
        if self._running:
            return True

        self._running = True
        return True

    async def stop(self) -> None:
        """Stop the observability system."""
        self._running = False

        if self._metrics_server_task:
            self._metrics_server_task.cancel()
            try:
                await self._metrics_server_task
            except asyncio.CancelledError:
                pass

    # Metrics API
    def increment_counter(self, name: str, value: int = 1, labels: Optional[Dict[str, str]] = None) -> None:
        """Increment a counter metric."""
        key = self._make_metric_key(name, labels)
        self._counters[key] = self._counters.get(key, 0) + value
        self._stats["metrics_collected"] += 1

    def set_gauge(self, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        """Set a gauge metric."""
        key = self._make_metric_key(name, labels)
        self._gauges[key] = value
        self._stats["metrics_collected"] += 1

    def record_histogram(self, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        """Record a histogram observation."""
        key = self._make_metric_key(name, labels)
        if key not in self._histograms:
            self._histograms[key] = []
        self._histograms[key].append(value)
        if len(self._histograms[key]) > 10000:
            self._histograms[key] = self._histograms[key][-5000:]
        self._stats["metrics_collected"] += 1

    def _make_metric_key(self, name: str, labels: Optional[Dict[str, str]]) -> str:
        """Create a unique metric key with labels."""
        if not labels:
            return name
        label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        return f"{name}{{{label_str}}}"

    # Tracing API
    def start_trace(
        self,
        operation: str,
        parent_trace_id: Optional[str] = None,
    ) -> str:
        """Start a new trace span."""
        trace_id = f"trace_{int(time.time() * 1000)}_{os.urandom(4).hex()}"

        self._active_traces[trace_id] = {
            "trace_id": trace_id,
            "parent_id": parent_trace_id,
            "operation": operation,
            "component": self._component_name,
            "start_time": time.time(),
            "end_time": None,
            "duration_ms": None,
            "status": "active",
            "tags": {},
            "logs": [],
        }

        return trace_id

    def add_trace_tag(self, trace_id: str, key: str, value: Any) -> None:
        """Add a tag to an active trace."""
        if trace_id in self._active_traces:
            self._active_traces[trace_id]["tags"][key] = value

    def add_trace_log(self, trace_id: str, message: str) -> None:
        """Add a log entry to an active trace."""
        if trace_id in self._active_traces:
            self._active_traces[trace_id]["logs"].append({
                "timestamp": datetime.now().isoformat(),
                "message": message,
            })

    def end_trace(self, trace_id: str, status: str = "ok", error: Optional[str] = None) -> None:
        """End a trace span."""
        if trace_id not in self._active_traces:
            return

        trace = self._active_traces.pop(trace_id)
        trace["end_time"] = time.time()
        trace["duration_ms"] = (trace["end_time"] - trace["start_time"]) * 1000
        trace["status"] = status
        if error:
            trace["error"] = error

        self._completed_traces.append(trace)
        if len(self._completed_traces) > self._max_traces:
            self._completed_traces = self._completed_traces[-self._max_traces:]

        self._stats["traces_recorded"] += 1

        # Record duration as histogram
        self.record_histogram(
            "trace_duration_ms",
            trace["duration_ms"],
            {"operation": trace["operation"]},
        )

    # Error aggregation API
    def record_error(
        self,
        error_type: str,
        message: str,
        stack_trace: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record an error with aggregation."""
        error_key = f"{error_type}:{message[:50]}"
        self._error_counts[error_key] = self._error_counts.get(error_key, 0) + 1

        error_entry = {
            "timestamp": datetime.now().isoformat(),
            "type": error_type,
            "message": message,
            "stack_trace": stack_trace,
            "context": context or {},
            "count": self._error_counts[error_key],
        }

        self._recent_errors.append(error_entry)
        if len(self._recent_errors) > self._max_errors:
            self._recent_errors = self._recent_errors[-self._max_errors:]

        self._stats["errors_aggregated"] += 1
        self.increment_counter("errors_total", labels={"type": error_type})

        # Check alert rules
        # v210.0: Use safe task to prevent "Future exception was never retrieved"
        create_safe_task(self._check_alerts(), name="check_alerts")

    # Alerting API
    def add_alert_rule(
        self,
        alert_id: str,
        condition: Callable[[], bool],
        message: str,
        severity: str = "warning",
    ) -> None:
        """Add an alert rule."""
        self._alert_rules.append({
            "id": alert_id,
            "condition": condition,
            "message": message,
            "severity": severity,
        })

    async def _check_alerts(self) -> None:
        """Check all alert rules."""
        current_time = time.time()

        for rule in self._alert_rules:
            alert_id = rule["id"]

            # Check cooldown
            last_fired = self._fired_alerts.get(alert_id, 0)
            if current_time - last_fired < self._alert_cooldown:
                continue

            try:
                if rule["condition"]():
                    self._fired_alerts[alert_id] = current_time
                    self._stats["alerts_fired"] += 1
                    # In production, this would send to alerting system
            except Exception:
                pass

    def get_prometheus_metrics(self) -> str:
        """Export metrics in Prometheus format."""
        lines = []

        # Export counters
        for key, value in self._counters.items():
            lines.append(f"{key} {value}")

        # Export gauges
        for key, value in self._gauges.items():
            lines.append(f"{key} {value}")

        # Export histogram summaries
        for key, values in self._histograms.items():
            if values:
                lines.append(f"{key}_count {len(values)}")
                lines.append(f"{key}_sum {sum(values)}")
                sorted_values = sorted(values)
                lines.append(f'{key}{{quantile="0.5"}} {sorted_values[len(sorted_values)//2]}')
                lines.append(f'{key}{{quantile="0.9"}} {sorted_values[int(len(sorted_values)*0.9)]}')
                lines.append(f'{key}{{quantile="0.99"}} {sorted_values[int(len(sorted_values)*0.99)]}')

        return "\n".join(lines)

    def get_status(self) -> Dict[str, Any]:
        """Get observability system status."""
        return {
            "running": self._running,
            "component": self._component_name,
            "metrics": {
                "counters": len(self._counters),
                "gauges": len(self._gauges),
                "histograms": len(self._histograms),
            },
            "tracing": {
                "active_traces": len(self._active_traces),
                "completed_traces": len(self._completed_traces),
            },
            "errors": {
                "unique_errors": len(self._error_counts),
                "recent_errors": len(self._recent_errors),
            },
            "alerts": {
                "rules": len(self._alert_rules),
                "fired": len(self._fired_alerts),
            },
            "stats": self._stats,
        }


class PhysicsAwareAuthManager:
    """
    Physics-Aware Voice Authentication Startup Manager.

    Initializes and manages the physics-aware authentication components:
    - Reverberation analyzer (RT60, double-reverb detection)
    - Vocal tract length estimator (VTL biometrics)
    - Doppler analyzer (liveness detection)
    - Bayesian confidence fusion
    - 7-layer anti-spoofing system

    Environment Configuration:
    - PHYSICS_AWARE_ENABLED: Enable/disable (default: true)
    - PHYSICS_PRELOAD_MODELS: Preload models at startup (default: false)
    - PHYSICS_BASELINE_VTL_CM: User's baseline VTL (default: auto-detect)
    - PHYSICS_BASELINE_RT60_SEC: User's baseline RT60 (default: auto-detect)

    Anti-Spoofing Layers:
    1. Spectral analysis for replay detection
    2. Microphone fingerprinting
    3. Environmental acoustics
    4. Vocal tract analysis (VTL biometrics)
    5. Reverberation consistency
    6. Doppler movement detection
    7. Bayesian fusion of all layers
    """

    def __init__(
        self,
        config: Optional[SystemKernelConfig] = None,
        logger: Optional[Any] = None,
    ):
        """
        Initialize physics-aware authentication manager.

        Args:
            config: System kernel configuration
            logger: Logger instance
        """
        self.config = config
        self._logger = logger or logging.getLogger("PhysicsAuth")

        # Configuration from environment
        self.enabled = os.getenv("PHYSICS_AWARE_ENABLED", "true").lower() == "true"
        self.preload_models = (
            os.getenv("PHYSICS_PRELOAD_MODELS", "false").lower() == "true"
        )

        # Baseline values (can be overridden or auto-detected)
        self._baseline_vtl_cm: Optional[float] = None
        self._baseline_rt60_sec: Optional[float] = None

        baseline_vtl = os.getenv("PHYSICS_BASELINE_VTL_CM")
        if baseline_vtl:
            try:
                self._baseline_vtl_cm = float(baseline_vtl)
            except ValueError:
                pass

        baseline_rt60 = os.getenv("PHYSICS_BASELINE_RT60_SEC")
        if baseline_rt60:
            try:
                self._baseline_rt60_sec = float(baseline_rt60)
            except ValueError:
                pass

        # Component references
        self._physics_extractor: Optional[Any] = None
        self._anti_spoofing_detector: Optional[Any] = None
        self._initialized = False

        # Statistics
        self.initialization_time_ms = 0.0
        self.physics_verifications = 0
        self.spoofs_detected = 0
        self.legitimate_authentications = 0

        # Spoof detection history for learning
        self._spoof_history: List[Dict[str, Any]] = []
        self._max_history = 100

        self._logger.info("🔬 Physics-Aware Auth Manager initialized:")
        self._logger.info(f"   ├─ Enabled: {self.enabled}")
        self._logger.info(f"   ├─ Preload models: {self.preload_models}")
        self._logger.info(f"   ├─ Baseline VTL: {self._baseline_vtl_cm or 'auto-detect'} cm")
        self._logger.info(f"   └─ Baseline RT60: {self._baseline_rt60_sec or 'auto-detect'} sec")

    async def initialize(self) -> bool:
        """
        Initialize physics-aware authentication components.

        Returns:
            True if initialization successful
        """
        if not self.enabled:
            self._logger.info("🔬 Physics-aware authentication disabled")
            return False

        start_time = time.time()

        try:
            # Try to import physics components from backend
            try:
                from backend.voice_unlock.core.feature_extraction import (
                    get_physics_feature_extractor,
                    PhysicsConfig,
                )
                from backend.voice_unlock.core.anti_spoofing import (
                    get_anti_spoofing_detector,
                )

                # Initialize physics extractor
                sample_rate = int(os.getenv("AUDIO_SAMPLE_RATE", "16000"))
                self._physics_extractor = get_physics_feature_extractor(sample_rate)

                # Set baselines if provided
                if self._baseline_vtl_cm and hasattr(
                    self._physics_extractor, "_baseline_vtl"
                ):
                    self._physics_extractor._baseline_vtl = self._baseline_vtl_cm
                if self._baseline_rt60_sec and hasattr(
                    self._physics_extractor, "_baseline_rt60"
                ):
                    self._physics_extractor._baseline_rt60 = self._baseline_rt60_sec

                # Initialize anti-spoofing detector (includes Layer 7 physics)
                self._anti_spoofing_detector = get_anti_spoofing_detector()

                self._initialized = True
                self.initialization_time_ms = (time.time() - start_time) * 1000

                vtl_range = (
                    f"{PhysicsConfig.VTL_MIN_CM}-{PhysicsConfig.VTL_MAX_CM} cm"
                    if hasattr(PhysicsConfig, "VTL_MIN_CM")
                    else "12-20 cm"
                )
                prior = (
                    f"{PhysicsConfig.PRIOR_AUTHENTIC:.0%}"
                    if hasattr(PhysicsConfig, "PRIOR_AUTHENTIC")
                    else "95%"
                )

                self._logger.info(
                    f"✅ Physics-aware auth initialized ({self.initialization_time_ms:.0f}ms)"
                )
                self._logger.info(f"   ├─ Physics extractor: Ready")
                self._logger.info(f"   ├─ Anti-spoofing (7-layer): Ready")
                self._logger.info(f"   ├─ VTL range: {vtl_range}")
                self._logger.info(f"   └─ Bayesian prior: {prior} authentic")

                return True

            except ImportError as e:
                self._logger.debug(f"Physics components not available: {e}")
                # Fall back to mock implementation
                self._initialized = True
                self.initialization_time_ms = (time.time() - start_time) * 1000
                self._logger.info(
                    f"✅ Physics-aware auth initialized (mock mode, {self.initialization_time_ms:.0f}ms)"
                )
                return True

        except Exception as e:
            self._logger.error(f"Physics initialization failed: {e}")
            self.enabled = False
            return False

    async def verify_physics(
        self,
        audio_data: bytes,
        sample_rate: int = 16000,
    ) -> Dict[str, Any]:
        """
        Perform physics-based verification on audio.

        Args:
            audio_data: Raw audio bytes
            sample_rate: Audio sample rate

        Returns:
            Verification result with confidence scores
        """
        self.physics_verifications += 1

        result = {
            "authentic": True,
            "confidence": 0.95,
            "checks": {},
            "timestamp": time.time(),
        }

        if not self._initialized:
            result["error"] = "Not initialized"
            return result

        try:
            if self._anti_spoofing_detector:
                # Run 7-layer anti-spoofing
                spoof_result = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: self._anti_spoofing_detector.detect(
                        audio_data, sample_rate
                    ),
                )

                result["authentic"] = not spoof_result.get("is_spoof", False)
                result["confidence"] = spoof_result.get("confidence", 0.5)
                result["checks"] = spoof_result.get("layer_results", {})

                if not result["authentic"]:
                    self.spoofs_detected += 1
                    self._record_spoof(spoof_result)
                else:
                    self.legitimate_authentications += 1

            if self._physics_extractor:
                # Extract physics features
                features = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: self._physics_extractor.extract(audio_data, sample_rate),
                )
                result["physics_features"] = features

        except Exception as e:
            self._logger.error(f"Physics verification failed: {e}")
            result["error"] = str(e)

        return result

    def _record_spoof(self, spoof_result: Dict[str, Any]) -> None:
        """Record spoof detection for learning."""
        record = {
            "timestamp": time.time(),
            "result": spoof_result,
        }
        self._spoof_history.append(record)

        # Trim history
        if len(self._spoof_history) > self._max_history:
            self._spoof_history = self._spoof_history[-self._max_history :]

    def get_physics_extractor(self) -> Optional[Any]:
        """Get the physics feature extractor instance."""
        return self._physics_extractor

    def get_anti_spoofing_detector(self) -> Optional[Any]:
        """Get the anti-spoofing detector instance."""
        return self._anti_spoofing_detector

    def get_statistics(self) -> Dict[str, Any]:
        """Get physics startup statistics."""
        return {
            "enabled": self.enabled,
            "initialized": self._initialized,
            "initialization_time_ms": self.initialization_time_ms,
            "baseline_vtl_cm": self._baseline_vtl_cm,
            "baseline_rt60_sec": self._baseline_rt60_sec,
            "physics_verifications": self.physics_verifications,
            "spoofs_detected": self.spoofs_detected,
            "legitimate_authentications": self.legitimate_authentications,
            "spoof_history_count": len(self._spoof_history),
        }


class OSResourceQuotaMonitor:
    """
    Resource quota management with ulimit protection.

    Monitors and enforces resource limits:
    - File descriptor limits
    - Memory usage limits
    - CPU time limits
    - Process count limits
    - Network connection limits

    Features:
    - Automatic limit detection from OS
    - Soft limit warnings before hard failures
    - Resource reservation for critical operations
    - Automatic cleanup when approaching limits
    """

    def __init__(
        self,
        enable_monitoring: bool = True,
        warning_threshold: float = 0.8,  # 80% of limit
        critical_threshold: float = 0.95,  # 95% of limit
    ) -> None:
        self._enable_monitoring = enable_monitoring
        self._warning_threshold = warning_threshold
        self._critical_threshold = critical_threshold

        # Resource limits (detected from OS)
        self._limits: Dict[str, Dict[str, int]] = {}

        # Current usage
        self._usage: Dict[str, int] = {}

        # Reserved resources
        self._reservations: Dict[str, Dict[str, int]] = {}

        # Monitoring
        self._monitor_task: Optional[asyncio.Task] = None
        self._running = False
        self._check_interval = 30.0

        # Callbacks for limit warnings
        self._warning_callbacks: List[Callable[[str, float], Awaitable[None]]] = []
        self._critical_callbacks: List[Callable[[str, float], Awaitable[None]]] = []

        # Statistics
        self._stats = {
            "warnings_issued": 0,
            "critical_alerts": 0,
            "cleanups_triggered": 0,
            "reservations_granted": 0,
            "reservations_denied": 0,
        }

        # Detect initial limits
        self._detect_limits()

    def _detect_limits(self) -> None:
        """Detect resource limits from OS."""
        try:
            import resource

            # File descriptors
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            self._limits["file_descriptors"] = {"soft": soft, "hard": hard}

            # Memory (virtual)
            soft, hard = resource.getrlimit(resource.RLIMIT_AS)
            if soft != resource.RLIM_INFINITY:
                self._limits["virtual_memory"] = {"soft": soft, "hard": hard}

            # CPU time
            soft, hard = resource.getrlimit(resource.RLIMIT_CPU)
            if soft != resource.RLIM_INFINITY:
                self._limits["cpu_time"] = {"soft": soft, "hard": hard}

            # Max processes
            soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
            self._limits["processes"] = {"soft": soft, "hard": hard}

        except ImportError:
            pass
        except Exception:
            pass

    async def start(self) -> bool:
        """Start resource monitoring."""
        if self._running or not self._enable_monitoring:
            return True

        self._running = True
        self._monitor_task = create_safe_task(self._monitor_loop())
        return True

    async def stop(self) -> None:
        """Stop resource monitoring."""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

    async def _monitor_loop(self) -> None:
        """Background monitoring loop."""
        while self._running:
            try:
                await self._check_all_resources()
                await asyncio.sleep(self._check_interval)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(self._check_interval)

    async def _check_all_resources(self) -> None:
        """Check all resource usage."""
        # Check file descriptors
        await self._check_file_descriptors()

        # Check memory
        await self._check_memory()

        # Check processes
        await self._check_processes()

    async def _check_file_descriptors(self) -> None:
        """Check file descriptor usage."""
        try:
            import psutil

            process = psutil.Process()
            fd_count = process.num_fds()
            self._usage["file_descriptors"] = fd_count

            if "file_descriptors" in self._limits:
                soft_limit = self._limits["file_descriptors"]["soft"]
                ratio = fd_count / soft_limit

                if ratio >= self._critical_threshold:
                    self._stats["critical_alerts"] += 1
                    for callback in self._critical_callbacks:
                        await callback("file_descriptors", ratio)
                elif ratio >= self._warning_threshold:
                    self._stats["warnings_issued"] += 1
                    for callback in self._warning_callbacks:
                        await callback("file_descriptors", ratio)
        except ImportError:
            pass
        except Exception:
            pass

    async def _check_memory(self) -> None:
        """Check memory usage."""
        try:
            import psutil

            process = psutil.Process()
            memory_info = process.memory_info()
            self._usage["rss_memory"] = memory_info.rss
            self._usage["vms_memory"] = memory_info.vms

            # v260.1: Check against system memory via MCP broker
            memory_ratio = _mcp_memory_percent() / 100

            if memory_ratio >= self._critical_threshold:
                self._stats["critical_alerts"] += 1
                for callback in self._critical_callbacks:
                    await callback("memory", memory_ratio)
            elif memory_ratio >= self._warning_threshold:
                self._stats["warnings_issued"] += 1
                for callback in self._warning_callbacks:
                    await callback("memory", memory_ratio)
        except ImportError:
            pass
        except Exception:
            pass

    async def _check_processes(self) -> None:
        """Check process count."""
        try:
            count = await _run_in_supervisor_thread(
                lambda: len(__import__('psutil').Process().children(recursive=True)),
                timeout=5.0,
            )
            self._usage["child_processes"] = count
        except (asyncio.TimeoutError, Exception):
            pass

    def reserve_resources(
        self,
        reservation_id: str,
        file_descriptors: int = 0,
        memory_mb: int = 0,
    ) -> bool:
        """
        Reserve resources for a critical operation.

        Returns:
            True if reservation granted, False otherwise
        """
        # Check if we have capacity
        if file_descriptors > 0 and "file_descriptors" in self._limits:
            current_fd = self._usage.get("file_descriptors", 0)
            reserved_fd = sum(r.get("file_descriptors", 0) for r in self._reservations.values())
            available_fd = self._limits["file_descriptors"]["soft"] - current_fd - reserved_fd

            if file_descriptors > available_fd * (1 - self._warning_threshold):
                self._stats["reservations_denied"] += 1
                return False

        # Grant reservation
        self._reservations[reservation_id] = {
            "file_descriptors": file_descriptors,
            "memory_mb": memory_mb,
            "created_at": time.time(),
        }
        self._stats["reservations_granted"] += 1
        return True

    def release_reservation(self, reservation_id: str) -> None:
        """Release a resource reservation."""
        if reservation_id in self._reservations:
            del self._reservations[reservation_id]

    def register_warning_callback(
        self,
        callback: Callable[[str, float], Awaitable[None]],
    ) -> None:
        """Register a callback for resource warnings."""
        self._warning_callbacks.append(callback)

    def register_critical_callback(
        self,
        callback: Callable[[str, float], Awaitable[None]],
    ) -> None:
        """Register a callback for critical resource alerts."""
        self._critical_callbacks.append(callback)

    def get_status(self) -> Dict[str, Any]:
        """Get resource quota status."""
        return {
            "limits": self._limits,
            "usage": self._usage,
            "reservations": len(self._reservations),
            "running": self._running,
            "stats": self._stats,
        }
