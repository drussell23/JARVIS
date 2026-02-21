#!/usr/bin/env python3
"""
Enhanced Continuous Screen Analyzer for JARVIS
Memory-optimized real-time screen monitoring with Claude Vision integration
Optimized for 16GB RAM macOS systems
"""

import asyncio
import hashlib
import logging
import time
import os
import gc
import psutil
from difflib import SequenceMatcher
from typing import Dict, Any, Optional, Callable, List, Deque
from datetime import datetime, timedelta
from collections import deque
import types
import weakref
import numpy as np
from PIL import Image
import io
import base64

logger = logging.getLogger(__name__)


class _CallbackSet:
    """
    v264.0: Drop-in replacement for weakref.WeakSet that supports bound methods.

    Python's weakref.WeakSet cannot hold bound methods (types.MethodType) because
    they don't support __weakref__. Calling WeakSet.add(obj.method) silently fails
    with TypeError, making callback registration appear to succeed while actually
    doing nothing.

    This class uses:
    - weakref.WeakMethod for bound methods (Python 3.4+)
    - weakref.ref for regular callables (functions, objects with __weakref__)
    - Strong reference fallback for C functions / callables without __weakref__

    Dead references are cleaned lazily during iteration and explicitly via _sweep().
    """

    __slots__ = ('_weak_refs', '_strong_refs')

    def __init__(self):
        self._weak_refs: list = []    # weakref.ref / weakref.WeakMethod entries
        self._strong_refs: list = []  # non-weakrefable callbacks (require explicit discard)

    @staticmethod
    def _callbacks_equal(a, b) -> bool:
        """Compare callbacks correctly. Bound methods need == (compares __self__ + __func__),
        regular callables use identity."""
        if isinstance(a, types.MethodType) or isinstance(b, types.MethodType):
            return a == b
        return a is b

    def add(self, callback) -> None:
        """Add a callback. Idempotent — won't double-register the same live callback."""
        # Check all existing (weak + strong) for duplicates
        for cb in self:
            if self._callbacks_equal(cb, callback):
                return

        # Try weak reference first
        if isinstance(callback, types.MethodType):
            self._weak_refs.append(weakref.WeakMethod(callback))
            return
        try:
            self._weak_refs.append(weakref.ref(callback))
        except TypeError:
            # C extension functions, slots, etc. can't be weakly referenced.
            # Store as strong ref — requires explicit discard() for cleanup.
            self._strong_refs.append(callback)

    def discard(self, callback) -> None:
        """Remove a callback if present (from either weak or strong list).

        Dereferences each weak ref exactly once into a local to avoid:
        - 2N ephemeral MethodType allocations (WeakMethod creates new bound method per call)
        - Theoretical atomicity gap (referent collected between first and second deref)
        """
        alive = []
        for ref in self._weak_refs:
            obj = ref()
            if obj is not None and not self._callbacks_equal(obj, callback):
                alive.append(ref)
        self._weak_refs = alive
        self._strong_refs = [
            cb for cb in self._strong_refs
            if not self._callbacks_equal(cb, callback)
        ]

    def _sweep(self) -> None:
        """Remove dead weak references. Strong refs are never swept (require discard)."""
        self._weak_refs = [r for r in self._weak_refs if r() is not None]

    def __iter__(self):
        """Yield live callbacks from a snapshot.

        Uses copy-on-read to avoid mutating internal lists during iteration.
        This prevents interleaving hazards where add()/discard() modifies
        lists while a generator consumer is mid-iteration.
        """
        self._sweep()
        for ref in list(self._weak_refs):  # snapshot
            obj = ref()
            if obj is not None:
                yield obj
        for cb in list(self._strong_refs):  # snapshot
            yield cb

    def __len__(self) -> int:
        self._sweep()
        return len(self._weak_refs) + len(self._strong_refs)

    def __bool__(self) -> bool:
        self._sweep()
        return bool(self._weak_refs) or bool(self._strong_refs)

class MemoryAwareScreenAnalyzer:
    """
    Memory-optimized continuous screen monitoring system
    Fully configurable with no hardcoded values
    """
    
    def __init__(self, vision_handler, update_interval: Optional[float] = None):
        """
        Initialize memory-aware continuous screen analyzer
        
        Args:
            vision_handler: The vision action handler for Claude Vision
            update_interval: How often to capture screen (in seconds)
        """
        self.vision_handler = vision_handler
        
        # Load all configuration from environment variables
        self.config = {
            'update_interval': float(os.getenv('VISION_MONITOR_INTERVAL', str(update_interval or 3.0))),
            'max_captures_in_memory': int(os.getenv('VISION_MAX_CAPTURES', '10')),
            'capture_retention_seconds': int(os.getenv('VISION_CAPTURE_RETENTION', '300')),  # 5 minutes
            'cache_duration_seconds': float(os.getenv('VISION_CACHE_DURATION', '5.0')),
            # v251.1: Raised from 200→1500MB.  This checks PROCESS RSS (entire
            # JARVIS monolith), not this component alone.  A normal JARVIS
            # process loading ECAPA-TDNN + 60 neural mesh agents + learning DB
            # easily uses 700MB+.  200MB guaranteed the check always fails.
            'memory_limit_mb': int(os.getenv('VISION_MEMORY_LIMIT_MB', '1500')),
            'memory_check_interval': float(os.getenv('VISION_MEMORY_CHECK_INTERVAL', '10.0')),
            'low_memory_threshold_mb': int(os.getenv('VISION_LOW_MEMORY_MB', '2000')),  # 2GB free RAM
            'critical_memory_threshold_mb': int(os.getenv('VISION_CRITICAL_MEMORY_MB', '1000')),  # 1GB free RAM
            'dynamic_interval_enabled': os.getenv('VISION_DYNAMIC_INTERVAL', 'true').lower() == 'true',
            'min_interval_seconds': float(os.getenv('VISION_MIN_INTERVAL', '1.0')),
            'max_interval_seconds': float(os.getenv('VISION_MAX_INTERVAL', '10.0')),
            'content_similarity_threshold': float(
                os.getenv('VISION_CONTENT_SIMILARITY_THRESHOLD', '0.92')
            ),
            'event_dedup_window_seconds': float(
                os.getenv('VISION_EVENT_DEDUP_WINDOW_SECONDS', '6.0')
            ),
            'app_change_cooldown_seconds': float(
                os.getenv('VISION_APP_CHANGE_COOLDOWN_SECONDS', '1.0')
            ),
            'content_change_cooldown_seconds': float(
                os.getenv('VISION_CONTENT_CHANGE_COOLDOWN_SECONDS', '2.0')
            ),
            'semantic_event_cooldown_seconds': float(
                os.getenv('VISION_SEMANTIC_EVENT_COOLDOWN_SECONDS', '8.0')
            ),
            'capture_change_threshold': float(
                os.getenv('VISION_CAPTURE_CHANGE_THRESHOLD', '0.10')
            ),
            'callback_timeout_seconds': float(
                os.getenv('VISION_CALLBACK_TIMEOUT_SECONDS', '3.0')
            ),
        }
        
        self.is_monitoring = False
        self._monitoring_task = None
        self._memory_monitor_task = None
        self._cleanup_task = None
        
        # Use circular buffer for screen captures (memory efficient)
        self.capture_history: Deque[Dict[str, Any]] = deque(maxlen=self.config['max_captures_in_memory'])
        
        # Current screen state with weak references
        self.current_screen_state = {
            'last_capture': None,
            'last_analysis': None,
            'current_app': None,
            'visible_elements': [],
            'context': {},
            'timestamp': None,
            'quick_app': None,
            'content_fingerprint': None,
            'content_text': '',
            'capture_signature': None,
        }
        
        # v264.0: Callbacks via _CallbackSet (supports bound methods via WeakMethod).
        # Previous weakref.WeakSet silently dropped bound methods (TypeError on add).
        self.event_callbacks = {
            'app_changed': _CallbackSet(),
            'content_changed': _CallbackSet(),
            'weather_visible': _CallbackSet(),
            'error_detected': _CallbackSet(),
            'user_needs_help': _CallbackSet(),
            'memory_warning': _CallbackSet(),
            # v241.0: Extended callback types for ScreenAnalyzerBridge integration
            'notification_detected': _CallbackSet(),
            'meeting_detected': _CallbackSet(),
            'security_concern': _CallbackSet(),
            'screen_captured': _CallbackSet(),
        }
        
        # Performance optimization with size limits
        self._analysis_cache = {}  # Will be cleaned periodically
        self._cache_sizes = {}  # Track size of cached items
        
        # Memory tracking
        self.memory_stats = {
            'current_usage_mb': 0,
            'peak_usage_mb': 0,
            'captures_dropped': 0,
            'memory_warnings': 0
        }

        # Event dedup + observability
        self._event_last_emitted: Dict[str, float] = {}
        self._event_last_fingerprint: Dict[str, str] = {}
        self._event_stats = {
            'emitted': {event_name: 0 for event_name in self.event_callbacks},
            'suppressed': {event_name: 0 for event_name in self.event_callbacks},
        }
        
        # Dynamic interval adjustment
        self.current_interval = self.config['update_interval']
        self.last_memory_check = time.time()
        
        logger.info(f"Memory-Aware Screen Analyzer initialized with config: {self.config}")
    
    async def start_monitoring(self):
        """Start continuous screen monitoring with memory management"""
        if self.is_monitoring:
            logger.warning("Screen monitoring is already active")
            return
        
        self.is_monitoring = True
        
        # Start monitoring tasks
        self._monitoring_task = asyncio.create_task(self._monitoring_loop())
        self._memory_monitor_task = asyncio.create_task(self._memory_monitor_loop())
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        
        logger.info("Started memory-aware continuous screen monitoring")
    
    async def stop_monitoring(self):
        """Stop continuous screen monitoring and cleanup"""
        if not self.is_monitoring:
            return
        
        self.is_monitoring = False
        
        # Cancel all tasks
        tasks = [self._monitoring_task, self._memory_monitor_task, self._cleanup_task]
        for task in tasks:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        # Clear memory
        self._clear_caches()
        gc.collect()
        
        logger.info("Stopped continuous screen monitoring and cleaned up memory")
    
    async def _monitoring_loop(self):
        """Main monitoring loop with two-phase capture/analysis architecture.

        v259.0 architectural fix — split into two phases:

        Phase 1 (ALWAYS runs, ~7MB, never skipped):
          - Screenshot capture
          - Fingerprinting (SHA1 of 32×32 grayscale)
          - Focused app detection (NSWorkspace, zero API cost)
          - Frame-diff content change detection

        Phase 2 (CONDITIONAL, memory-gated):
          - Full Claude Vision / J-Prime LLaVA analysis
          - Only runs when content changed AND memory allows
          - Offloads to GCP when local memory tight but GCP VM ready

        Previous design gated the entire cycle (including the cheap Phase 1
        operations) behind a single memory check.  This caused captures to
        stop entirely on a 16GB Mac under normal load — the OOM bridge was
        told "I need 1500MB" when the actual capture cost is ~7MB.
        """
        while self.is_monitoring:
            try:
                # ── Phase 1: ALWAYS capture + lightweight analysis ─────
                phase1 = await self._phase1_capture_and_detect()
                if phase1 is None:
                    # capture_screen() returned None (display off, etc.)
                    await asyncio.sleep(self.current_interval)
                    continue

                needs_full = phase1.get('needs_full_analysis', False)

                # ── Phase 2: Full analysis (only when content changed) ─
                if needs_full:
                    await self._phase2_analyze_if_memory_allows(phase1)

                # Adjust interval based on memory if enabled
                if self.config['dynamic_interval_enabled']:
                    self._adjust_interval_based_on_memory()

                # Wait for next update
                await asyncio.sleep(self.current_interval)

            except RuntimeError as e:
                # v259.0: Detect vision-unavailable cooldown errors and
                # sleep for the cooldown duration instead of retrying
                # every 3 seconds (which generates 20 error logs in 60s).
                err_msg = str(e).lower()
                if 'temporarily unavailable' in err_msg or 'cooldown' in err_msg:
                    cooldown_sleep = float(os.getenv(
                        'VISION_COOLDOWN_SLEEP_SECONDS', '30.0'
                    ))
                    logger.info(
                        "Vision temporarily unavailable — sleeping %.0fs "
                        "(cooldown-aware, not retrying every cycle)",
                        cooldown_sleep,
                    )
                    await asyncio.sleep(cooldown_sleep)
                else:
                    logger.error("Error in monitoring loop: %s", e)
                    await asyncio.sleep(self.current_interval)

            except Exception as e:
                logger.error("Error in monitoring loop: %s", e)
                await asyncio.sleep(self.current_interval)

    async def _phase1_capture_and_detect(self) -> Optional[Dict[str, Any]]:
        """Phase 1: Lightweight capture + local detection (~7MB, never skip).

        Returns dict with capture data and whether full analysis is needed,
        or None if capture failed (display off, etc.).
        """
        capture_result = await self.vision_handler.capture_screen()
        if capture_result is None:
            return None

        current_time = time.time()

        # Convert to consistent format
        if hasattr(capture_result, 'success'):
            if not capture_result.success:
                return None
        screenshot = capture_result

        # Store capture with size tracking
        capture_data = {
            'timestamp': current_time,
            'result': screenshot,
            'size_bytes': self._estimate_capture_size(screenshot),
        }
        self.capture_history.append(capture_data)

        # Fingerprinting + signature for frame-diff
        capture_fingerprint = self._compute_capture_fingerprint(screenshot)
        previous_capture_signature = self.current_screen_state.get(
            'capture_signature'
        )
        capture_signature = self._compute_capture_signature(screenshot)
        if capture_signature is not None:
            self.current_screen_state['capture_signature'] = capture_signature

        await self._trigger_event('screen_captured', {
            'timestamp': current_time,
            'capture_size_bytes': capture_data['size_bytes'],
            'capture_fingerprint': capture_fingerprint,
            '_event_fingerprint': capture_fingerprint,
            '_event_cooldown_seconds': max(
                0.5, float(self.config['app_change_cooldown_seconds'])
            ),
        })

        # Quick analysis — focused app detection (zero API cost)
        quick_analysis = await self._quick_screen_analysis()
        previous_quick_app = self.current_screen_state.get('quick_app')
        current_quick_app = self._normalize_app_name(
            quick_analysis.get('current_app')
        )
        if current_quick_app:
            self.current_screen_state['quick_app'] = current_quick_app

        if (
            current_quick_app
            and previous_quick_app
            and current_quick_app != previous_quick_app
        ):
            await self._trigger_event('app_changed', {
                'app_name': current_quick_app,
                'previous_app': previous_quick_app,
                'window_title': self.current_screen_state.get(
                    'current_window', ''
                ),
                'analysis_source': 'quick_analysis',
                '_event_fingerprint': (
                    f"{previous_quick_app}->{current_quick_app}"
                ),
                '_event_cooldown_seconds': float(
                    self.config['app_change_cooldown_seconds']
                ),
            })

        # Frame-diff content change detection (lightweight, no API)
        frame_diff_changed = False
        if (
            previous_capture_signature is not None
            and capture_signature is not None
        ):
            capture_change_score = self._compute_capture_change_score(
                previous_capture_signature, capture_signature
            )
            if capture_change_score >= float(
                self.config['capture_change_threshold']
            ):
                frame_diff_changed = True
                await self._trigger_event('content_changed', {
                    'app': current_quick_app,
                    'previous_app': previous_quick_app,
                    'text': '',
                    'visual_elements': [],
                    'similarity': max(0.0, 1.0 - capture_change_score),
                    'analysis_source': 'capture_signature_diff',
                    'capture_change_score': capture_change_score,
                    '_event_fingerprint': (
                        f"capture_diff:{current_quick_app or 'unknown'}"
                    ),
                    '_event_cooldown_seconds': float(
                        self.config['content_change_cooldown_seconds']
                    ),
                })

        needs_full = self._needs_full_analysis(quick_analysis)

        return {
            'screenshot': screenshot,
            'quick_analysis': quick_analysis,
            'current_quick_app': current_quick_app,
            'previous_quick_app': previous_quick_app,
            'needs_full_analysis': needs_full or frame_diff_changed,
            'timestamp': current_time,
        }

    async def _phase2_analyze_if_memory_allows(
        self, phase1: Dict[str, Any]
    ) -> None:
        """Phase 2: Full analysis, gated by memory for analysis cost only.

        Checks whether we can afford the analysis (~50MB for API payload +
        processing).  If local memory is tight, offloads to GCP LLaVA
        via PrimeClient.  If both are unavailable, skips analysis only —
        Phase 1 data (capture, fingerprint, app detection) is already saved.
        """
        # v259.0: estimated_mb reflects ANALYSIS cost (~50MB), not the
        # entire pipeline (was 1500MB — the process RSS limit).
        _analysis_mb = int(os.getenv('VISION_ANALYSIS_ESTIMATED_MB', '50'))
        mem_ok, gcp_endpoint = await self._check_analysis_memory(_analysis_mb)

        if gcp_endpoint:
            # GCP VM available — offload heavy analysis
            await self._analyze_via_cloud(
                phase1['screenshot'], gcp_endpoint, phase1
            )
        elif mem_ok:
            # Local memory OK — run full Claude Vision analysis
            await self._run_full_analysis(phase1)
        else:
            # Both unavailable — skip analysis, Phase 1 data is preserved
            if not getattr(self, '_analysis_skip_logged', False):
                logger.info(
                    "Skipping full analysis (memory tight, GCP unavailable) "
                    "— captures and lightweight detection continue normally"
                )
                self._analysis_skip_logged = True

    async def _check_analysis_memory(
        self, analysis_mb: int
    ) -> tuple:
        """Check memory specifically for the analysis phase.

        v259.0: Separate from capture memory check.  Only gates the
        expensive analysis operation, not the cheap capture.

        Returns:
            (can_proceed_locally: bool, gcp_endpoint: Optional[str])
        """
        _oom_timeout = float(
            os.getenv("JARVIS_VISION_OOM_CHECK_TIMEOUT", "2.0")
        )
        try:
            from core.gcp_oom_prevention_bridge import (
                check_memory_before_heavy_init,
            )
            result = await asyncio.wait_for(
                check_memory_before_heavy_init(
                    component="vision_analysis",
                    estimated_mb=analysis_mb,
                    auto_offload=False,
                ),
                timeout=_oom_timeout,
            )

            if result.can_proceed_locally:
                # Reset flags on recovery
                self._analysis_skip_logged = False
                self._memory_warned = False
                return True, None

            # Check if GCP VM can handle analysis
            gcp_endpoint = None
            if result.gcp_vm_ready and result.gcp_vm_ip:
                gcp_endpoint = result.gcp_vm_ip  # Raw IP — PrimeClient handles port
                if not getattr(self, '_cloud_offload_logged', False):
                    logger.info(
                        "OOM Bridge: offloading vision analysis to GCP VM at "
                        "%s (avail=%.1fGB, tier=%s)",
                        result.gcp_vm_ip,
                        result.available_ram_gb,
                        getattr(result, 'degradation_tier', 'unknown'),
                    )
                    self._cloud_offload_logged = True

            if not gcp_endpoint and not getattr(self, '_memory_warned', False):
                logger.warning(
                    "OOM Bridge: analysis %s (avail=%.1fGB, tier=%s, "
                    "gcp_ready=%s)",
                    result.decision.value,
                    result.available_ram_gb,
                    getattr(result, 'degradation_tier', 'unknown'),
                    result.gcp_vm_ready,
                )
                self._memory_warned = True

            return False, gcp_endpoint

        except (ImportError, asyncio.TimeoutError, Exception) as e:
            logger.debug(
                "OOM bridge unavailable: %s — using psutil fallback", e
            )
            # v259.0: Sync fallback uses analysis-specific check, not the
            # process RSS limit that always fails on a loaded system.
            return self._check_analysis_memory_sync(analysis_mb), None

    def _check_analysis_memory_sync(self, analysis_mb: int) -> bool:
        """Sync fallback for analysis memory check.

        v259.0: Uses available system RAM vs analysis cost, NOT the
        process RSS limit (which always exceeds 1500MB on a loaded
        system and caused the old _check_memory_available to always
        return False).

        Also implements hysteresis: once we skip, only resume when
        available RAM exceeds the threshold by a 200MB margin.
        This prevents oscillation when RSS hovers near threshold.
        """
        available_mb = self._get_available_memory_mb()
        # Minimum system RAM to run analysis: analysis cost + 500MB headroom
        min_available = analysis_mb + 500
        # Hysteresis: if we were skipping, require extra margin to resume
        _hysteresis_mb = 200
        if getattr(self, '_analysis_skip_logged', False):
            min_available += _hysteresis_mb

        return available_mb >= min_available

    async def _run_full_analysis(self, phase1: Dict[str, Any]) -> None:
        """Run full Claude Vision analysis locally (Phase 2 body).

        Extracted from the old _capture_and_analyze so it can be called
        independently after Phase 1 capture is already done.
        """
        previous_content_text = self.current_screen_state.get(
            'content_text', ''
        )
        previous_fingerprint = self.current_screen_state.get(
            'content_fingerprint'
        )
        previous_app = (
            self.current_screen_state.get('current_app')
            or phase1.get('previous_quick_app')
        )

        # Perform full Claude Vision analysis
        analysis = await self._full_screen_analysis()

        # Update screen state
        self._update_screen_state(analysis)

        content_text = self.current_screen_state.get('content_text', '')
        content_fingerprint = self.current_screen_state.get(
            'content_fingerprint'
        )
        current_app = (
            self.current_screen_state.get('current_app')
            or self.current_screen_state.get('quick_app')
        )
        similarity = self._compare_text_similarity(
            previous_content_text, content_text
        )
        content_threshold = max(
            0.1,
            min(1.0, float(self.config['content_similarity_threshold']))
        )
        app_changed = bool(
            previous_app and current_app and previous_app != current_app
        )
        content_changed = (
            previous_fingerprint is None
            or content_fingerprint != previous_fingerprint
        )

        if content_changed and (
            app_changed or similarity < content_threshold
        ):
            await self._trigger_event('content_changed', {
                'app': current_app,
                'previous_app': previous_app,
                'text': content_text,
                'visual_elements': self.current_screen_state.get(
                    'visible_elements', []
                ),
                'similarity': similarity,
                'analysis': analysis,
                '_event_fingerprint': content_fingerprint or '',
                '_event_cooldown_seconds': float(
                    self.config['content_change_cooldown_seconds']
                ),
            })

        # Trigger relevant callbacks
        await self._process_screen_events(analysis)

        # Reset skip flag on successful analysis
        self._analysis_skip_logged = False
    
    async def _memory_monitor_loop(self):
        """Monitor memory usage and trigger warnings"""
        while self.is_monitoring:
            try:
                self._update_memory_stats()
                
                # Check if we're exceeding limits
                available_mb = self._get_available_memory_mb()
                
                if available_mb < self.config['critical_memory_threshold_mb']:
                    # Critical memory - emergency cleanup
                    logger.warning(f"Critical memory: {available_mb}MB available")
                    await self._emergency_cleanup()
                    await self._trigger_event('memory_warning', {
                        'level': 'critical',
                        'available_mb': available_mb
                    })
                elif available_mb < self.config['low_memory_threshold_mb']:
                    # Low memory - normal cleanup
                    logger.info(f"Low memory: {available_mb}MB available")
                    self._clear_old_captures()
                    await self._trigger_event('memory_warning', {
                        'level': 'low',
                        'available_mb': available_mb
                    })
                
                await asyncio.sleep(self.config['memory_check_interval'])
                
            except Exception as e:
                logger.error(f"Error in memory monitor: {e}")
                await asyncio.sleep(self.config['memory_check_interval'])
    
    async def _cleanup_loop(self):
        """Periodic cleanup of old data"""
        while self.is_monitoring:
            try:
                # Clean old captures
                self._clear_old_captures()
                
                # Clean cache
                self._clean_cache()
                
                # Force garbage collection
                gc.collect()
                
                # Wait before next cleanup
                await asyncio.sleep(60.0)  # Cleanup every minute
                
            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}")
                await asyncio.sleep(60.0)
    
    def _check_memory_available(self) -> bool:
        """Check if enough memory is available for capture"""
        available_mb = self._get_available_memory_mb()
        process_mb = self._get_process_memory_mb()
        
        # Check system memory
        if available_mb < self.config['critical_memory_threshold_mb']:
            return False
        
        # v251.1: Check process memory against limit.  Only log once per
        # threshold crossing to avoid log spam (was logging every check cycle).
        if process_mb > self.config['memory_limit_mb']:
            if not getattr(self, '_memory_warned', False):
                logger.warning(
                    "Process memory %.0fMB exceeds limit %dMB — skipping captures",
                    process_mb, self.config['memory_limit_mb'],
                )
                self._memory_warned = True
            return False
        else:
            self._memory_warned = False
        
        return True

    async def _check_memory_available_async(self) -> tuple:
        """Async memory check with OOM bridge consultation.

        v255.1: Runtime check consults OOM Prevention Bridge for cloud-aware
        decision with 6-tier graceful degradation. Falls back to sync psutil
        check if bridge unavailable.

        v241.0: Returns (can_proceed_locally, gcp_endpoint_or_none) tuple.
        When local memory is insufficient but a GCP VM is ready, returns
        (False, endpoint_url) so the caller can offload capture to cloud.

        Returns:
            Tuple of (can_proceed: bool, gcp_endpoint: Optional[str])
        """
        _oom_timeout = float(os.getenv("JARVIS_VISION_OOM_CHECK_TIMEOUT", "2.0"))
        try:
            from core.gcp_oom_prevention_bridge import check_memory_before_heavy_init
            result = await asyncio.wait_for(
                check_memory_before_heavy_init(
                    component="vision_system",
                    estimated_mb=int(self.config.get('memory_limit_mb', 500)),
                    auto_offload=False,
                ),
                timeout=_oom_timeout,
            )

            if result.can_proceed_locally:
                # Reset warning flag on recovery
                if getattr(self, '_memory_warned', False):
                    self._memory_warned = False
                return True, None

            # v241.0: When local memory is insufficient, check if GCP VM
            # can handle the capture instead of just skipping it.
            gcp_endpoint = None
            if result.gcp_vm_ready and result.gcp_vm_ip:
                gcp_endpoint = f"http://{result.gcp_vm_ip}:8010/api/vision_capture"
                if not getattr(self, '_cloud_offload_logged', False):
                    logger.info(
                        "OOM Bridge: offloading vision capture to GCP VM at %s "
                        "(avail=%.1fGB, tier=%s)",
                        result.gcp_vm_ip,
                        result.available_ram_gb,
                        getattr(result, 'degradation_tier', 'unknown'),
                    )
                    self._cloud_offload_logged = True

            # Log skip only when no cloud fallback available
            if not gcp_endpoint and not getattr(self, '_memory_warned', False):
                logger.warning(
                    "OOM Bridge: vision %s (avail=%.1fGB, tier=%s, gcp_ready=%s)",
                    result.decision.value,
                    result.available_ram_gb,
                    getattr(result, 'degradation_tier', 'unknown'),
                    result.gcp_vm_ready,
                )
                self._memory_warned = True

            return False, gcp_endpoint

        except (ImportError, asyncio.TimeoutError, Exception) as e:
            logger.debug("OOM bridge unavailable: %s — falling back to psutil", e)
            return self._check_memory_available(), None

    def _get_available_memory_mb(self) -> float:
        """Get available system memory in MB"""
        return psutil.virtual_memory().available / 1024 / 1024
    
    def _get_process_memory_mb(self) -> float:
        """Get current process memory usage in MB"""
        process = psutil.Process()
        return process.memory_info().rss / 1024 / 1024
    
    def _update_memory_stats(self):
        """Update memory statistics"""
        current_mb = self._get_process_memory_mb()
        self.memory_stats['current_usage_mb'] = current_mb
        self.memory_stats['peak_usage_mb'] = max(self.memory_stats['peak_usage_mb'], current_mb)
    
    def _adjust_interval_based_on_memory(self):
        """Dynamically adjust capture interval based on memory pressure"""
        available_mb = self._get_available_memory_mb()
        
        if available_mb < self.config['critical_memory_threshold_mb']:
            # Critical - use maximum interval
            self.current_interval = self.config['max_interval_seconds']
        elif available_mb < self.config['low_memory_threshold_mb']:
            # Low memory - increase interval
            ratio = available_mb / self.config['low_memory_threshold_mb']
            range_size = self.config['max_interval_seconds'] - self.config['min_interval_seconds']
            self.current_interval = self.config['max_interval_seconds'] - (ratio * range_size)
        else:
            # Normal memory - use configured interval
            self.current_interval = self.config['update_interval']
        
        # Clamp to configured range
        self.current_interval = max(
            self.config['min_interval_seconds'],
            min(self.current_interval, self.config['max_interval_seconds'])
        )
    
    async def _capture_and_analyze(self):
        """Legacy entry point — delegates to Phase 1 + Phase 2.

        v259.0: Kept for backward compatibility.  New callers should use
        _phase1_capture_and_detect() + _phase2_analyze_if_memory_allows().
        """
        try:
            phase1 = await self._phase1_capture_and_detect()
            if phase1 is None:
                return
            if phase1.get('needs_full_analysis', False):
                await self._phase2_analyze_if_memory_allows(phase1)
        except Exception as e:
            logger.error("Error capturing/analyzing screen: %s", e)

    async def _analyze_via_cloud(
        self,
        screenshot: Any,
        gcp_vm_ip: str,
        phase1: Dict[str, Any],
    ) -> None:
        """v259.0: Offload analysis to GCP LLaVA via PrimeClient.

        Screenshot is already captured locally in Phase 1.  This method
        encodes it as base64 JPEG and sends it to the J-Prime LLaVA
        vision server (port 8001) using PrimeClient.send_vision_request(),
        which produces OpenAI-compatible multimodal messages.

        Previous implementation (v241.0 _capture_via_cloud) had three bugs:
        - Wrong port (8010 instead of 8001)
        - Wrong format (raw PNG bytes instead of base64 JSON)
        - Re-captured screenshot (already done in Phase 1)

        Results are fed back into the local event pipeline so downstream
        consumers see no difference between local and cloud analysis.
        """
        _cloud_timeout = float(
            os.getenv("JARVIS_VISION_CLOUD_TIMEOUT", "120.0")
        )
        try:
            from backend.core.prime_client import get_prime_client

            client = await get_prime_client()
            if client is None:
                logger.warning(
                    "PrimeClient unavailable — cannot offload to GCP"
                )
                return

            # Encode screenshot as base64 JPEG for PrimeClient
            img = screenshot
            if isinstance(img, Image.Image):
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=85)
                image_b64 = base64.b64encode(buf.getvalue()).decode('ascii')
            elif hasattr(img, 'tobytes'):
                # numpy array — convert via PIL
                pil_img = Image.fromarray(np.asarray(img))
                buf = io.BytesIO()
                pil_img.save(buf, format="JPEG", quality=85)
                image_b64 = base64.b64encode(buf.getvalue()).decode('ascii')
            else:
                logger.debug(
                    "Cloud analysis: unsupported screenshot type %s",
                    type(img),
                )
                return

            # Send to J-Prime LLaVA vision server via PrimeClient
            prompt = (
                "Analyze the current screen and provide:\n"
                "1. Currently active application\n"
                "2. Key UI elements visible\n"
                "3. Any error messages or dialogs\n"
                "4. What the user appears to be doing\n"
                "5. Any text content that might be relevant\n"
                "Be concise but thorough."
            )

            response = await asyncio.wait_for(
                client.send_vision_request(
                    image_base64=image_b64,
                    prompt=prompt,
                    max_tokens=512,
                    temperature=0.1,
                    timeout=_cloud_timeout,
                ),
                timeout=_cloud_timeout + 5.0,
            )

            if not response or not response.content:
                logger.warning("GCP vision analysis returned empty response")
                return

            # Build analysis dict matching local format
            analysis = {
                'success': True,
                'description': response.content,
                'timestamp': time.time(),
                'raw_data': {
                    'source': 'gcp_llava',
                    'gcp_vm_ip': gcp_vm_ip,
                    'latency_ms': getattr(response, 'latency_ms', 0),
                    'model': getattr(response, 'model', 'llava'),
                },
            }

            # Update screen state from cloud analysis
            self._update_screen_state(analysis)

            current_app = (
                self.current_screen_state.get('current_app')
                or phase1.get('current_quick_app', '')
            )
            previous_app = phase1.get('previous_quick_app', '')

            await self._trigger_event('content_changed', {
                'app': current_app,
                'previous_app': previous_app,
                'text': response.content,
                'visual_elements': [],
                'analysis_source': 'gcp_llava',
                'analysis': analysis,
                '_event_fingerprint': self._fingerprint_text(
                    response.content
                ),
                '_event_cooldown_seconds': float(
                    self.config['content_change_cooldown_seconds']
                ),
            })

            # Process semantic events from analysis
            await self._process_screen_events(analysis)

            # Reset flags on success
            self._cloud_offload_logged = False
            self._analysis_skip_logged = False

        except ImportError:
            logger.debug(
                "PrimeClient not available for cloud vision offload"
            )
        except asyncio.TimeoutError:
            logger.warning(
                "GCP vision analysis timed out after %.0fs", _cloud_timeout
            )
        except Exception as e:
            logger.warning(
                "GCP vision analysis failed: %s — will retry next cycle", e
            )

    # Legacy alias for backward compatibility
    async def _capture_via_cloud(self, gcp_endpoint: str) -> None:
        """Deprecated — use _analyze_via_cloud instead."""
        logger.debug(
            "_capture_via_cloud is deprecated; use _analyze_via_cloud"
        )

    # ── Public API for cache sharing (v259.0) ───────────────────

    def get_latest_capture(
        self, max_age_seconds: float = 2.0
    ) -> Optional[Any]:
        """Return the most recent screenshot if fresh enough.

        v259.0: Allows VisionCommandHandler to reuse a recent capture
        from the continuous monitoring loop instead of taking a new
        screenshot.  This eliminates 200-500ms of redundant screen
        capture for on-demand "can you see my screen?" requests.

        Args:
            max_age_seconds: Maximum age of the capture to consider
                fresh.  Default 2.0s balances freshness vs cache hits
                (monitoring loop runs every 3s).  Configurable via
                VISION_CACHE_FRESHNESS_SECONDS.

        Returns:
            PIL Image / screenshot object if a fresh capture exists,
            None otherwise (caller should capture a new one).
        """
        freshness = float(
            os.getenv('VISION_CACHE_FRESHNESS_SECONDS', str(max_age_seconds))
        )
        if not self.capture_history:
            return None

        latest = self.capture_history[-1]
        age = time.time() - latest.get('timestamp', 0)
        if age <= freshness:
            return latest.get('result')
        return None

    def _normalize_app_name(self, app_name: Any) -> str:
        """Normalize app name from quick/full analyzers."""
        if app_name is None:
            return ''
        value = str(app_name).strip()
        if value.lower() in ('', 'unknown', 'none', 'null'):
            return ''
        return value

    def _compute_capture_fingerprint(self, screenshot: Any) -> str:
        """Compute a stable lightweight fingerprint for the captured frame."""
        try:
            if isinstance(screenshot, Image.Image):
                sample = screenshot.convert('L').resize((32, 32))
                return hashlib.sha1(sample.tobytes()).hexdigest()[:20]
            if isinstance(screenshot, np.ndarray):
                array = screenshot
                if array.ndim == 3:
                    array = np.mean(array, axis=2)
                sample = Image.fromarray(array.astype(np.uint8)).resize((32, 32))
                return hashlib.sha1(sample.tobytes()).hexdigest()[:20]
            if hasattr(screenshot, 'tobytes'):
                raw = screenshot.tobytes()
                return hashlib.sha1(raw[:4096]).hexdigest()[:20]
        except Exception:
            pass
        return hashlib.sha1(str(type(screenshot)).encode()).hexdigest()[:20]

    def _compute_capture_signature(self, screenshot: Any) -> Optional[np.ndarray]:
        """Build a normalized grayscale signature for frame-diff scoring."""
        try:
            if isinstance(screenshot, Image.Image):
                sample = screenshot.convert('L').resize((24, 24))
                return np.array(sample, dtype=np.float32) / 255.0
            if isinstance(screenshot, np.ndarray):
                array = screenshot
                if array.ndim == 3:
                    array = np.mean(array, axis=2)
                sample = Image.fromarray(array.astype(np.uint8)).convert('L').resize((24, 24))
                return np.array(sample, dtype=np.float32) / 255.0
        except Exception:
            return None
        return None

    def _compute_capture_change_score(
        self,
        previous_signature: np.ndarray,
        current_signature: np.ndarray,
    ) -> float:
        """Compute normalized average pixel delta between two frame signatures."""
        if previous_signature.shape != current_signature.shape:
            return 1.0
        delta = np.abs(previous_signature - current_signature)
        return float(np.clip(np.mean(delta), 0.0, 1.0))

    def _extract_analysis_text(self, analysis: Dict[str, Any]) -> str:
        """Extract comparable text from a full analysis result."""
        description = str(analysis.get('description', ''))
        raw_data = analysis.get('raw_data', {})
        text_chunks: List[str] = [description]

        if isinstance(raw_data, dict):
            for key in (
                'text',
                'ocr_text',
                'summary',
                'active_app',
                'window_title',
                'current_task',
            ):
                value = raw_data.get(key)
                if value:
                    text_chunks.append(str(value))
            for key in ('notifications', 'errors', 'warnings', 'messages'):
                value = raw_data.get(key)
                if isinstance(value, list):
                    text_chunks.extend(str(item) for item in value[:10])
        elif raw_data:
            text_chunks.append(str(raw_data))

        merged = " ".join(" ".join(text_chunks).split())
        return merged[:8000]

    def _extract_visual_elements(self, analysis: Dict[str, Any]) -> List[Any]:
        """Extract visual elements from analysis raw data if available."""
        raw_data = analysis.get('raw_data', {})
        if isinstance(raw_data, dict):
            for key in ('visual_elements', 'elements', 'ui_elements'):
                value = raw_data.get(key)
                if isinstance(value, list):
                    return value
        return []

    def _fingerprint_text(self, value: str) -> str:
        """Create deterministic fingerprint for textual content."""
        normalized = " ".join((value or '').strip().lower().split())
        return hashlib.sha1(normalized.encode()).hexdigest()[:20]

    def _compare_text_similarity(self, old_text: str, new_text: str) -> float:
        """Compare text bodies to estimate semantic change."""
        if not old_text and not new_text:
            return 1.0
        if not old_text or not new_text:
            return 0.0
        return SequenceMatcher(None, old_text[:4000], new_text[:4000]).ratio()
    
    def _estimate_capture_size(self, capture_data: Any) -> int:
        """Estimate memory size of capture"""
        # Try to get actual size if it's an image
        try:
            if hasattr(capture_data, 'size'):
                # PIL Image
                width, height = capture_data.size
                return width * height * 3  # RGB channels
            elif hasattr(capture_data, 'shape'):
                # Numpy array
                return capture_data.nbytes
            else:
                # Basic estimation
                return 1024 * 1024  # Assume 1MB per capture
        except Exception:
            return 1024 * 1024  # Default 1MB
    
    async def _quick_screen_analysis(self) -> Dict[str, Any]:
        """Detect major screen changes using local APIs (zero API cost).

        v237.3: Uses macOS NSWorkspace/osascript for focused app detection
        instead of Claude API call. This runs every capture cycle (~3s) so
        it MUST be local — describe_screen() is reserved for full analysis
        only (gated by _needs_full_analysis()).
        """
        app_name = await self._get_focused_app_local()

        return {
            'current_app': app_name,
            'timestamp': time.time()
        }

    async def _get_focused_app_local(self) -> str:
        """Get focused application name using local macOS APIs.

        Zero API cost. Fallback chain:
        1. NSWorkspace (PyObjC) — <1ms, most reliable
        2. osascript via subprocess — ~50ms, reliable fallback
        3. 'Unknown' — safe default (triggers full analysis on first cycle)
        """
        # Method 1: PyObjC NSWorkspace (fastest, <1ms)
        try:
            from AppKit import NSWorkspace
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            if app:
                name = app.localizedName()
                if name:
                    return name
        except ImportError:
            pass
        except Exception:
            pass

        # Method 2: osascript fallback (~50ms)
        try:
            import subprocess
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    ['osascript', '-e',
                     'tell application "System Events" to get name of '
                     'first application process whose frontmost is true'],
                    capture_output=True, text=True, timeout=2
                )
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            pass

        return 'Unknown'
    
    async def _full_screen_analysis(self) -> Dict[str, Any]:
        """Perform comprehensive screen analysis with caching"""
        # Check cache first
        cache_key = f"full_analysis_{int(time.time() / self.config['cache_duration_seconds'])}"
        if cache_key in self._analysis_cache:
            return self._analysis_cache[cache_key]
        
        # Comprehensive analysis prompt
        params = {
            'query': '''Analyze the current screen and provide:
1. Currently active application
2. Key UI elements visible
3. Any weather information if Weather app is visible
4. Any error messages or dialogs
5. What the user appears to be doing
6. Any text content that might be relevant

Be concise but thorough.''',
            '_is_continuous': True,  # v236.0: Route to J-Prime LLaVA
        }

        result = await self.vision_handler.describe_screen(params)

        # describe_screen() may return a dict (claude_vision_analyzer_main)
        # or an object with .success/.description/.data attributes
        # (VisionActionResult from vision_action_handler). Handle both.
        if isinstance(result, dict):
            _success = result.get('success', False)
            _description = result.get('description', '') if _success else ''
            _raw_data = result.get('data', {})
        else:
            _success = getattr(result, 'success', False)
            _description = getattr(result, 'description', '') if _success else ''
            _raw_data = getattr(result, 'data', {}) if hasattr(result, 'data') else {}

        analysis = {
            'success': _success,
            'description': _description,
            'timestamp': time.time(),
            'raw_data': _raw_data if isinstance(_raw_data, dict) else {}
        }
        
        # Cache with size tracking
        self._cache_analysis(cache_key, analysis)
        
        return analysis
    
    def _cache_analysis(self, key: str, analysis: Dict[str, Any]):
        """Cache analysis with size tracking"""
        # Estimate size
        size = len(str(analysis).encode())
        
        # Check if adding would exceed memory limit
        total_cache_size = sum(self._cache_sizes.values())
        if total_cache_size + size > self.config['memory_limit_mb'] * 0.1 * 1024 * 1024:  # Use 10% for cache
            # Remove oldest entries
            self._clean_cache(force=True)
        
        self._analysis_cache[key] = analysis
        self._cache_sizes[key] = size
    
    def _clean_cache(self, force: bool = False):
        """Clean old cache entries"""
        current_time = time.time()
        keys_to_remove = []
        
        for key in list(self._analysis_cache.keys()):
            # Extract timestamp from key
            try:
                key_time = int(key.split('_')[-1]) * self.config['cache_duration_seconds']
                if force or (current_time - key_time > self.config['cache_duration_seconds'] * 2):
                    keys_to_remove.append(key)
            except Exception:
                keys_to_remove.append(key)  # Remove malformed keys
        
        for key in keys_to_remove:
            self._analysis_cache.pop(key, None)
            self._cache_sizes.pop(key, None)
    
    def _clear_old_captures(self):
        """Clear captures older than retention period"""
        if not self.capture_history:
            return
        
        current_time = time.time()
        retention_seconds = self.config['capture_retention_seconds']
        
        # Remove old captures
        while self.capture_history:
            oldest = self.capture_history[0]
            if current_time - oldest['timestamp'] > retention_seconds:
                self.capture_history.popleft()
                self.memory_stats['captures_dropped'] += 1
            else:
                break
    
    async def _emergency_cleanup(self):
        """Emergency cleanup when memory is critical"""
        logger.warning("Performing emergency memory cleanup")
        
        # Clear all captures except the latest
        if len(self.capture_history) > 1:
            latest = self.capture_history[-1]
            self.capture_history.clear()
            self.capture_history.append(latest)
        
        # Clear all caches
        self._clear_caches()
        
        # Force garbage collection
        gc.collect()
        
        self.memory_stats['memory_warnings'] += 1
    
    def _clear_caches(self):
        """Clear all caches"""
        self._analysis_cache.clear()
        self._cache_sizes.clear()
    
    def _needs_full_analysis(self, quick_analysis: Dict[str, Any]) -> bool:
        """Determine if full analysis is needed"""
        # Always analyze if no previous state
        if not self.current_screen_state['last_analysis']:
            return True
        
        # Check if app changed
        quick_app = self._normalize_app_name(quick_analysis.get('current_app'))
        tracked_app = self._normalize_app_name(
            self.current_screen_state.get('quick_app')
            or self.current_screen_state.get('current_app')
        )
        if quick_app and tracked_app and quick_app != tracked_app:
            return True
        
        # Check if enough time has passed (configurable)
        last_analysis_time = self.current_screen_state.get('timestamp', 0)
        full_analysis_interval = float(os.getenv('VISION_FULL_ANALYSIS_INTERVAL', '10.0'))
        if time.time() - last_analysis_time > full_analysis_interval:
            return True
        
        return False
    
    def _update_screen_state(self, analysis: Dict[str, Any]):
        """Update internal screen state"""
        content_text = self._extract_analysis_text(analysis)
        current_app = self._normalize_app_name(self._extract_current_app(analysis))
        self.current_screen_state.update({
            'last_analysis': analysis.get('description', ''),
            'timestamp': analysis.get('timestamp', time.time()),
            'current_app': current_app,
            'content_text': content_text,
            'content_fingerprint': self._fingerprint_text(content_text),
            'visible_elements': self._extract_visual_elements(analysis),
            'context': analysis.get('raw_data', {}),
        })
    
    def _extract_current_app(self, analysis: Dict[str, Any]) -> Optional[str]:
        """Extract current application from analysis"""
        description = analysis.get('description', '').lower()
        
        # Load app detection patterns from environment or use defaults
        app_patterns = os.getenv('VISION_APP_PATTERNS')
        if app_patterns:
            try:
                import json
                apps = json.loads(app_patterns)
            except Exception:
                apps = self._get_default_app_patterns()
        else:
            apps = self._get_default_app_patterns()
        
        for app_name, keywords in apps.items():
            if any(keyword in description for keyword in keywords):
                return app_name
        
        return None
    
    def _get_default_app_patterns(self) -> Dict[str, List[str]]:
        """Get default app detection patterns"""
        return {
            'weather': ['weather app', 'weather.app'],
            'safari': ['safari'],
            'chrome': ['chrome', 'google chrome'],
            'vscode': ['vs code', 'visual studio code', 'vscode', 'cursor'],
            'terminal': ['terminal', 'iterm'],
            'finder': ['finder'],
            'mail': ['mail app', 'mail.app'],
            'messages': ['messages', 'imessage']
        }
    
    async def _process_screen_events(self, analysis: Dict[str, Any]):
        """Process screen events and trigger callbacks"""
        content_text = self._extract_analysis_text(analysis)
        description = content_text.lower()
        semantic_cooldown = float(self.config['semantic_event_cooldown_seconds'])
        
        # Check for weather visibility
        if 'weather' in description and any(word in description for word in ['temperature', 'degrees', '°']):
            await self._trigger_event('weather_visible', {
                'analysis': analysis,
                'weather_info': self._extract_weather_info(description),
                '_event_fingerprint': self._fingerprint_text(description),
                '_event_cooldown_seconds': semantic_cooldown,
            })
        
        # Check for errors
        error_keywords = ['error', 'failed', 'exception', 'crash', 'not responding']
        if any(word in description for word in error_keywords):
            await self._trigger_event('error_detected', {
                'analysis': analysis,
                'error_context': description,
                'error_type': self._extract_first_match(description, error_keywords),
                '_event_fingerprint': self._fingerprint_text(f"error|{description}"),
                '_event_cooldown_seconds': semantic_cooldown,
            })

        notification_keywords = [
            'notification',
            'new message',
            'unread',
            'mentions you',
            'badge',
        ]
        if any(word in description for word in notification_keywords):
            await self._trigger_event('notification_detected', {
                'type': 'visual_notification',
                'source_app': self._infer_notification_source(description),
                'title': '',
                'content': content_text[:300],
                '_event_fingerprint': self._fingerprint_text(
                    f"notification|{content_text}"
                ),
                '_event_cooldown_seconds': semantic_cooldown,
            })

        meeting_keywords = [
            'meeting',
            'calendar',
            'zoom',
            'google meet',
            'microsoft teams',
            'starts in',
        ]
        if any(word in description for word in meeting_keywords):
            minutes_until = self._extract_minutes_until(description)
            await self._trigger_event('meeting_detected', {
                'title': '',
                'start_time': '',
                'minutes_until': minutes_until if minutes_until is not None else 0,
                'platform': self._infer_meeting_platform(description),
                '_event_fingerprint': self._fingerprint_text(f"meeting|{content_text}"),
                '_event_cooldown_seconds': semantic_cooldown,
            })

        security_keywords = [
            'security',
            'password',
            'authenticate',
            'verification code',
            'malware',
            'suspicious',
            'permission',
            'grant access',
        ]
        if any(word in description for word in security_keywords):
            await self._trigger_event('security_concern', {
                'type': 'security_prompt',
                'description': content_text[:400],
                'severity': self._infer_security_severity(description),
                'recommended_action': 'review_before_confirming',
                '_event_fingerprint': self._fingerprint_text(
                    f"security|{content_text}"
                ),
                '_event_cooldown_seconds': semantic_cooldown,
            })

        help_keywords = [
            'please wait',
            'loading',
            'stuck',
            'not responding',
            'try again',
            'connection lost',
        ]
        if any(word in description for word in help_keywords):
            await self._trigger_event('user_needs_help', {
                'context': content_text[:300],
                'reason': self._extract_first_match(description, help_keywords),
                '_event_fingerprint': self._fingerprint_text(f"help|{content_text}"),
                '_event_cooldown_seconds': semantic_cooldown,
            })

    def _extract_first_match(self, description: str, keywords: List[str]) -> str:
        """Return first matching keyword in description."""
        for keyword in keywords:
            if keyword in description:
                return keyword
        return "unknown"

    def _infer_notification_source(self, description: str) -> str:
        """Best-effort source application inference for notifications."""
        source_map = {
            'slack': 'Slack',
            'discord': 'Discord',
            'teams': 'Teams',
            'mail': 'Mail',
            'messages': 'Messages',
            'calendar': 'Calendar',
        }
        for token, source in source_map.items():
            if token in description:
                return source
        return self.current_screen_state.get('quick_app') or 'unknown'

    def _infer_meeting_platform(self, description: str) -> str:
        """Best-effort meeting platform inference."""
        platform_map = {
            'zoom': 'Zoom',
            'google meet': 'Google Meet',
            'meet': 'Google Meet',
            'teams': 'Teams',
            'webex': 'Webex',
        }
        for token, platform in platform_map.items():
            if token in description:
                return platform
        return 'unknown'

    def _extract_minutes_until(self, description: str) -> Optional[int]:
        """Extract 'meeting in X minutes' style hints."""
        import re

        match = re.search(r'(\d{1,3})\s*(minute|min)s?\b', description)
        if not match:
            return None
        try:
            return int(match.group(1))
        except Exception:
            return None

    def _infer_security_severity(self, description: str) -> str:
        """Infer severity for detected security concerns."""
        if any(token in description for token in ('malware', 'suspicious', 'breach')):
            return 'high'
        if any(token in description for token in ('password', 'authenticate', 'permission')):
            return 'medium'
        return 'low'
    
    def _extract_weather_info(self, description: str) -> Optional[str]:
        """Extract weather information from screen description"""
        # Use the weather parser if available
        try:
            from utils.weather_response_parser import WeatherResponseParser
            parser = WeatherResponseParser()
            return parser.extract_weather_info(description)
        except ImportError:
            # Fallback to simple extraction
            return description if 'weather' in description.lower() else None
    
    async def _trigger_event(self, event_type: str, data: Dict[str, Any]) -> bool:
        """Trigger event callbacks using weak references with dedup suppression."""
        if event_type not in self.event_callbacks:
            return False

        payload = dict(data)
        dedup_fingerprint = str(
            payload.pop('_event_fingerprint', '')
        ) or self._fingerprint_text(f"{event_type}|{payload}")
        cooldown_seconds = float(
            payload.pop(
                '_event_cooldown_seconds',
                self.config['event_dedup_window_seconds'],
            )
        )

        now = time.monotonic()
        last_ts = self._event_last_emitted.get(event_type, 0.0)
        last_fingerprint = self._event_last_fingerprint.get(event_type)
        if (
            last_fingerprint == dedup_fingerprint
            and now - last_ts < max(0.0, cooldown_seconds)
        ):
            self._event_stats['suppressed'][event_type] += 1
            return False

        self._event_last_emitted[event_type] = now
        self._event_last_fingerprint[event_type] = dedup_fingerprint
        self._event_stats['emitted'][event_type] += 1

        # Resolve live callbacks from _CallbackSet to iterate safely.
        # v264.0: Uses call-then-check-result pattern instead of iscoroutinefunction()
        # heuristic. This correctly handles async bound methods recreated by WeakMethod,
        # functools.partial wrappers, and decorated callbacks that strip coroutine markers.
        callbacks = list(self.event_callbacks[event_type])
        async_tasks = []
        for callback in callbacks:
            try:
                result = callback(payload)
                if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                    async_tasks.append(
                        asyncio.create_task(
                            self._invoke_async_callback_from_awaitable(
                                result, event_type
                            )
                        )
                    )
            except Exception as e:
                logger.error(f"Error in callback for {event_type}: {e}")

        if async_tasks:
            results = await asyncio.gather(*async_tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logger.error("Async callback error for %s: %s", event_type, result)
        return True

    async def _invoke_async_callback_from_awaitable(
        self,
        awaitable,
        event_type: str,
    ) -> None:
        """Await an already-created coroutine/future with timeout isolation.

        v264.0: Used by _trigger_event's call-then-check-result pattern.
        The callback has already been called and returned a coroutine — we just
        need to await it with a timeout guard.
        """
        timeout = max(0.1, float(self.config.get('callback_timeout_seconds', 3.0)))
        try:
            await asyncio.wait_for(asyncio.ensure_future(awaitable), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "Async callback timeout for %s after %.2fs", event_type, timeout
            )

    def register_callback(self, event_type: str, callback: Callable):
        """Register a callback for specific events"""
        if event_type in self.event_callbacks:
            self.event_callbacks[event_type].add(callback)
            logger.info(f"Registered callback for {event_type}")
            return
        logger.warning("Unknown callback type requested: %s", event_type)

    def unregister_callback(self, event_type: str, callback: Callable) -> None:
        """Unregister a callback for specific events."""
        if event_type in self.event_callbacks:
            self.event_callbacks[event_type].discard(callback)

    def get_event_stats(self) -> Dict[str, Any]:
        """Get callback emission and suppression stats."""
        return {
            'emitted': self._event_stats['emitted'].copy(),
            'suppressed': self._event_stats['suppressed'].copy(),
            'last_emitted_monotonic': self._event_last_emitted.copy(),
        }
    
    async def get_current_screen_context(self) -> Dict[str, Any]:
        """Get current screen context for queries"""
        # If we have recent analysis, return it
        if self.current_screen_state['last_analysis']:
            age = time.time() - self.current_screen_state['timestamp']
            if age < self.config['cache_duration_seconds']:
                return self.current_screen_state
        
        # Otherwise, do a fresh analysis
        analysis = await self._full_screen_analysis()
        self._update_screen_state(analysis)
        return self.current_screen_state
    
    async def query_screen_for_weather(self) -> Optional[str]:
        """
        Query screen specifically for weather information
        Memory-efficient approach
        """
        # First check if Weather app is already visible
        context = await self.get_current_screen_context()
        
        if context.get('current_app') == 'weather':
            # Weather app is already open, just read it
            params = {
                'query': 'Read the weather information from the Weather app. What is the temperature, conditions, and forecast?',
                '_is_continuous': True,  # v236.0: Route to J-Prime LLaVA
            }
        else:
            # Need to open Weather app first
            try:
                from system_control import MacOSController
                controller = MacOSController()

                # Open Weather app
                controller.open_application("Weather")
                await asyncio.sleep(2.0)  # Wait for it to open

                # Now read the weather
                params = {
                    'query': 'The Weather app should now be open. Read the weather information: temperature, conditions, and forecast for today.',
                    '_is_continuous': True,  # v236.0: Route to J-Prime LLaVA
                }
            except ImportError:
                return None

        result = await self.vision_handler.describe_screen(params)

        # Handle both dict and object return types from describe_screen()
        if isinstance(result, dict):
            _success = result.get('success', False)
            _description = result.get('description', '')
        else:
            _success = getattr(result, 'success', False)
            _description = getattr(result, 'description', '')

        if _success:
            # Parse the weather info
            weather_info = self._extract_weather_info(_description)
            return weather_info
        
        return None
    
    def get_memory_stats(self) -> Dict[str, Any]:
        """Get current memory statistics"""
        return {
            **self.memory_stats,
            'captures_in_memory': len(self.capture_history),
            'cache_entries': len(self._analysis_cache),
            'current_interval': self.current_interval,
            'available_system_mb': self._get_available_memory_mb(),
            'callback_types': sorted(self.event_callbacks.keys()),
            'event_stats': self.get_event_stats(),
        }

# Backward compatibility alias
ContinuousScreenAnalyzer = MemoryAwareScreenAnalyzer
