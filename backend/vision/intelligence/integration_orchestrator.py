#!/usr/bin/env python3
"""
Integration Orchestrator - Core Processing Pipeline for JARVIS Vision
Brings Intelligence and Efficiency Together with Dynamic Resource Management
Total Memory Budget: 1.2GB (dynamically allocated)
"""

import asyncio
import time
import psutil
import gc
import os
import logging
from typing import Dict, List, Optional, Any, Tuple, Union, Callable
from dataclasses import dataclass, field
from datetime import datetime
from collections import OrderedDict
from enum import Enum
from concurrent.futures import ThreadPoolExecutor

# Import managed executor for clean shutdown
try:
    from core.thread_manager import ManagedThreadPoolExecutor
    _HAS_MANAGED_EXECUTOR = True
except ImportError:
    _HAS_MANAGED_EXECUTOR = False

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)


class SystemMode(Enum):
    """Operating modes based on memory pressure"""
    NORMAL = "normal"          # < 60% memory usage
    PRESSURE = "pressure"      # 60-80% memory usage  
    CRITICAL = "critical"      # > 80% memory usage
    EMERGENCY = "emergency"    # > 95% memory usage


class ComponentState(Enum):
    """Component operational state"""
    ACTIVE = "active"
    REDUCED = "reduced"
    SUSPENDED = "suspended"
    DISABLED = "disabled"


@dataclass
class MemoryAllocation:
    """Dynamic memory allocation for components"""
    component: str
    allocated_mb: float
    used_mb: float = 0.0
    priority: int = 5  # 1-10, higher is more important
    min_mb: float = 10.0
    max_mb: float = 200.0
    can_reduce: bool = True
    reduction_factor: float = 0.7  # Reduce to 70% in pressure mode


@dataclass
class ProcessingMetrics:
    """Metrics for pipeline performance"""
    stage_times: Dict[str, float] = field(default_factory=dict)
    memory_usage: Dict[str, float] = field(default_factory=dict)
    cache_hits: int = 0
    cache_misses: int = 0
    predictions_used: int = 0
    api_calls_saved: int = 0
    total_time: float = 0.0
    system_mode: SystemMode = SystemMode.NORMAL


class IntegrationOrchestrator:
    """
    Orchestrates the complete vision processing pipeline with intelligent resource management
    """
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Initialize the orchestrator with dynamic configuration"""
        self.config = self._build_config(config)
        
        # Memory allocations (Total: 1.2GB)
        self.memory_allocations = self._initialize_memory_allocations()
        
        # Component states
        self.component_states = {
            'intelligence': {},
            'optimization': {},
            'buffer': {}
        }
        
        # System state
        self.system_mode = SystemMode.NORMAL
        self.last_memory_check = time.time()
        self.memory_check_interval = float(os.getenv('MEMORY_CHECK_INTERVAL', '5.0'))
        
        # Component references (lazy loaded)
        self.components = {
            # Intelligence Systems
            'vsms': None,
            'scene_graph': None,
            'temporal_context': None,
            'activity_recognition': None,
            'goal_inference': None,
            'workflow_patterns': None,
            'anomaly_detection': None,
            'intervention_engine': None,
            'solution_bank': None,
            
            # Optimization Systems
            'quadtree': None,
            'semantic_cache': None,
            'predictive_engine': None,
            'bloom_filter': None
        }
        
        # Processing queue
        self.processing_queue = asyncio.Queue(maxsize=self.config['max_queue_size'])
        if _HAS_MANAGED_EXECUTOR:

            self.executor = ManagedThreadPoolExecutor(max_workers=self.config['max_workers'], name='pool')

        else:

            self.executor = ThreadPoolExecutor(max_workers=self.config['max_workers'])
        
        # Metrics tracking
        self.current_metrics = ProcessingMetrics()
        
        logger.info(f"Integration Orchestrator initialized with {self.config['total_memory_mb']}MB budget")
    
    def _build_config(self, custom_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Build configuration from environment and custom settings"""
        # Get actual available system memory
        vm = psutil.virtual_memory()
        total_system_mb = vm.total / (1024 * 1024)  # Total RAM in MB
        available_mb = vm.available / (1024 * 1024)  # Available RAM in MB
        
        # Calculate dynamic memory budget based on available RAM
        # Use 30% of available RAM for vision system (conservative)
        dynamic_budget = int(available_mb * 0.3)
        
        # But respect maximum limits to prevent over-allocation
        max_budget = min(dynamic_budget, 3000)  # Cap at 3GB max
        
        logger.info(f"System RAM: {total_system_mb:.0f}MB total, {available_mb:.0f}MB available")
        logger.info(f"Dynamic vision budget: {max_budget}MB (30% of available)")
        
        config = {
            # Memory settings - dynamically calculated
            'total_memory_mb': max_budget,
            'intelligence_memory_mb': int(max_budget * 0.5),  # 50% for intelligence
            'optimization_memory_mb': int(max_budget * 0.38), # 38% for optimization
            'buffer_memory_mb': int(max_budget * 0.12),      # 12% for buffer
            
            # Operating thresholds
            'pressure_threshold': float(os.getenv('MEMORY_PRESSURE_THRESHOLD', '0.6')),
            'critical_threshold': float(os.getenv('MEMORY_CRITICAL_THRESHOLD', '0.8')),
            'emergency_threshold': float(os.getenv('MEMORY_EMERGENCY_THRESHOLD', '0.95')),
            
            # Processing settings
            'max_queue_size': int(os.getenv('MAX_PROCESSING_QUEUE', '50')),
            'max_workers': int(os.getenv('MAX_WORKER_THREADS', '4')),
            'batch_size': int(os.getenv('PROCESSING_BATCH_SIZE', '5')),
            
            # Component settings
            'enable_all_components': os.getenv('ENABLE_ALL_COMPONENTS', 'true').lower() == 'true',
            'adaptive_quality': os.getenv('ADAPTIVE_QUALITY', 'true').lower() == 'true',
            'aggressive_caching': os.getenv('AGGRESSIVE_CACHING', 'true').lower() == 'true',
        }
        
        # Apply custom config
        if custom_config:
            config.update(custom_config)
        
        return config
    
    def _initialize_memory_allocations(self) -> Dict[str, MemoryAllocation]:
        """Initialize memory allocations dynamically based on available budget"""
        # Get our dynamic budgets
        intelligence_budget = self.config['intelligence_memory_mb']
        optimization_budget = self.config['optimization_memory_mb']
        
        # Calculate proportional allocations
        allocations = {
            # Intelligence Systems (proportional to budget)
            'vsms': MemoryAllocation('vsms', intelligence_budget * 0.15, priority=9, min_mb=20.0),
            'scene_graph': MemoryAllocation('scene_graph', intelligence_budget * 0.10, priority=8, min_mb=15.0),
            'temporal_context': MemoryAllocation('temporal_context', intelligence_budget * 0.20, priority=7, min_mb=25.0),
            'activity_recognition': MemoryAllocation('activity_recognition', intelligence_budget * 0.10, priority=7, min_mb=10.0),
            'goal_inference': MemoryAllocation('goal_inference', intelligence_budget * 0.08, priority=6, min_mb=10.0),
            'workflow_patterns': MemoryAllocation('workflow_patterns', intelligence_budget * 0.12, priority=6, min_mb=15.0),
            'anomaly_detection': MemoryAllocation('anomaly_detection', intelligence_budget * 0.07, priority=5, min_mb=10.0),
            'intervention_engine': MemoryAllocation('intervention_engine', intelligence_budget * 0.08, priority=5, min_mb=10.0),
            'solution_bank': MemoryAllocation('solution_bank', intelligence_budget * 0.10, priority=4, min_mb=10.0),
            
            # Optimization Systems (proportional to budget)
            'quadtree': MemoryAllocation('quadtree', optimization_budget * 0.11, priority=8, min_mb=10.0),
            'semantic_cache': MemoryAllocation('semantic_cache', optimization_budget * 0.54, priority=9, min_mb=25.0),
            'predictive_engine': MemoryAllocation('predictive_engine', optimization_budget * 0.33, priority=7, min_mb=15.0),
            'bloom_filter': MemoryAllocation('bloom_filter', 10.0, priority=6, min_mb=5.0, can_reduce=False),
            
            # Operating Buffer (140MB total)
            'frame_buffer': MemoryAllocation('frame_buffer', 60.0, priority=10, min_mb=20.0, can_reduce=False),
            'processing_workspace': MemoryAllocation('processing_workspace', 50.0, priority=9, min_mb=10.0),
            'emergency_reserve': MemoryAllocation('emergency_reserve', 30.0, priority=10, min_mb=30.0, can_reduce=False),
        }
        
        return allocations
    
    async def process_frame(self, 
                          frame: Union[np.ndarray, Image.Image],
                          context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Process a single frame through the complete pipeline
        
        Args:
            frame: Input frame as numpy array or PIL Image
            context: Optional context information
            
        Returns:
            Processed result with all intelligence and optimizations applied
        """
        start_time = time.time()
        metrics = ProcessingMetrics()
        
        # Check and update system mode
        await self._update_system_mode()
        metrics.system_mode = self.system_mode
        
        # Stage 1: Visual Input Processing
        stage_start = time.time()
        processed_frame = await self._process_visual_input(frame, metrics)
        metrics.stage_times['visual_input'] = time.time() - stage_start
        
        # Stage 2: Spatial Analysis (Quadtree)
        stage_start = time.time()
        spatial_analysis = await self._analyze_spatial(processed_frame, metrics)
        metrics.stage_times['spatial_analysis'] = time.time() - stage_start
        
        # Stage 3: State Understanding (VSMS)
        stage_start = time.time()
        state_understanding = await self._understand_state(
            processed_frame, spatial_analysis, context, metrics
        )
        metrics.stage_times['state_understanding'] = time.time() - stage_start
        
        # Stage 4: Intelligence Processing (IUS)
        stage_start = time.time()
        intelligence_result = await self._process_intelligence(
            state_understanding, metrics
        )
        metrics.stage_times['intelligence_processing'] = time.time() - stage_start
        
        # Stage 5: Cache Checking (LSH + Bloom)
        stage_start = time.time()
        cache_result = await self._check_caches(
            processed_frame, intelligence_result, metrics
        )
        if cache_result:
            metrics.cache_hits += 1
            metrics.api_calls_saved += 1
            metrics.stage_times['cache_checking'] = time.time() - stage_start
            metrics.total_time = time.time() - start_time
            return self._build_result(cache_result, metrics)
        metrics.cache_misses += 1
        metrics.stage_times['cache_checking'] = time.time() - stage_start
        
        # Stage 6: Prediction Engine (Markov)
        stage_start = time.time()
        prediction_result = await self._generate_predictions(
            state_understanding, intelligence_result, metrics
        )
        if prediction_result and prediction_result.get('confidence', 0) > 0.8:
            metrics.predictions_used += 1
            metrics.api_calls_saved += 1
            metrics.stage_times['prediction_engine'] = time.time() - stage_start
            metrics.total_time = time.time() - start_time
            return self._build_result(prediction_result, metrics)
        metrics.stage_times['prediction_engine'] = time.time() - stage_start
        
        # Stage 7: API Decision (Only if needed)
        stage_start = time.time()
        api_result = await self._make_api_decision(
            processed_frame, spatial_analysis, intelligence_result, metrics
        )
        metrics.stage_times['api_decision'] = time.time() - stage_start
        
        # Stage 8: Response Integration
        stage_start = time.time()
        integrated_result = await self._integrate_response(
            api_result, state_understanding, intelligence_result, metrics
        )
        metrics.stage_times['response_integration'] = time.time() - stage_start
        
        # Stage 9: Proactive Intelligence (PIS)
        stage_start = time.time()
        proactive_result = await self._apply_proactive_intelligence(
            integrated_result, metrics
        )
        metrics.stage_times['proactive_intelligence'] = time.time() - stage_start
        
        metrics.total_time = time.time() - start_time
        return self._build_result(proactive_result, metrics)
    
    async def _update_system_mode(self):
        """Update system mode based on actual available system memory"""
        current_time = time.time()
        if current_time - self.last_memory_check < self.memory_check_interval:
            return
        
        self.last_memory_check = current_time
        
        # Get actual system memory stats
        vm = psutil.virtual_memory()
        available_gb = vm.available / (1024 * 1024 * 1024)  # Available RAM in GB
        percent_used = vm.percent / 100.0  # System memory usage as fraction
        
        # Also check our process memory
        process = psutil.Process()
        process_mb = process.memory_info().rss / (1024 * 1024)
        
        logger.debug(f"Memory check: {available_gb:.1f}GB available, {percent_used:.1%} system used, process using {process_mb:.0f}MB")
        
        # Determine mode based on available system memory
        if available_gb < 1.0:  # Less than 1GB available
            new_mode = SystemMode.EMERGENCY
        elif available_gb < 2.0:  # Less than 2GB available
            new_mode = SystemMode.CRITICAL
        elif available_gb < 3.0:  # Less than 3GB available
            new_mode = SystemMode.PRESSURE
        else:
            new_mode = SystemMode.NORMAL
        
        # Apply mode changes if needed
        if new_mode != self.system_mode:
            logger.info(f"System mode changed: {self.system_mode.value} -> {new_mode.value}")
            await self._apply_system_mode(new_mode)
            self.system_mode = new_mode
    
    async def _apply_system_mode(self, mode: SystemMode):
        """Apply resource adjustments based on system mode"""
        if mode == SystemMode.NORMAL:
            # Restore full allocations
            for alloc in self.memory_allocations.values():
                alloc.allocated_mb = alloc.max_mb
        
        elif mode == SystemMode.PRESSURE:
            # Reduce cache sizes by 30%
            for name, alloc in self.memory_allocations.items():
                if alloc.can_reduce and name in ['semantic_cache', 'temporal_context']:
                    alloc.allocated_mb = max(alloc.min_mb, alloc.max_mb * 0.7)
        
        elif mode == SystemMode.CRITICAL:
            # Aggressive reductions
            for name, alloc in self.memory_allocations.items():
                if alloc.can_reduce:
                    alloc.allocated_mb = max(alloc.min_mb, alloc.max_mb * 0.5)
            # Disable low-priority components
            await self._disable_components(['solution_bank', 'workflow_patterns'])
        
        elif mode == SystemMode.EMERGENCY:
            # Emergency mode - minimal operation
            for name, alloc in self.memory_allocations.items():
                if alloc.can_reduce:
                    alloc.allocated_mb = alloc.min_mb
            # Keep only essential components
            await self._disable_components([
                'solution_bank', 'workflow_patterns', 'goal_inference',
                'anomaly_detection', 'intervention_engine', 'predictive_engine'
            ])
            # Force garbage collection
            gc.collect()
    
    async def _disable_components(self, component_names: List[str]):
        """Disable specified components to save memory"""
        for name in component_names:
            if name in self.components and self.components[name] is not None:
                # Call cleanup if available
                if hasattr(self.components[name], 'cleanup'):
                    await self.components[name].cleanup()
                self.components[name] = None
                logger.info(f"Disabled component: {name}")
    
    async def _process_visual_input(self, frame: Any, metrics: ProcessingMetrics) -> np.ndarray:
        """Stage 1: Process visual input"""
        # Convert to numpy if PIL
        if isinstance(frame, Image.Image):
            frame = np.array(frame)
        
        # Record memory usage
        metrics.memory_usage['visual_input'] = frame.nbytes / 1024 / 1024  # MB
        
        return frame
    
    async def _analyze_spatial(self, frame: np.ndarray, metrics: ProcessingMetrics) -> Dict[str, Any]:
        """Stage 2: Spatial analysis using Quadtree"""
        if self.system_mode == SystemMode.EMERGENCY:
            return {'regions': [], 'skipped': True}
        
        # Lazy load quadtree
        if self.components['quadtree'] is None:
            try:
                from .quadtree_spatial_intelligence import get_quadtree_spatial_intelligence
                self.components['quadtree'] = get_quadtree_spatial_intelligence()
            except Exception as e:
                logger.warning(f"Could not load quadtree: {e}")
                return {'regions': [], 'error': str(e)}
        
        # Analyze with quadtree
        try:
            quadtree = self.components['quadtree']
            regions = await quadtree.analyze_frame(frame)
            
            # Filter by importance based on mode
            importance_threshold = {
                SystemMode.NORMAL: 0.3,
                SystemMode.PRESSURE: 0.5,
                SystemMode.CRITICAL: 0.7,
                SystemMode.EMERGENCY: 0.9
            }[self.system_mode]
            
            important_regions = [r for r in regions if r['importance'] >= importance_threshold]
            
            return {
                'regions': important_regions,
                'total_regions': len(regions),
                'coverage_ratio': sum(r['area'] for r in important_regions) / (frame.shape[0] * frame.shape[1])
            }
        except Exception as e:
            logger.error(f"Spatial analysis failed: {e}")
            return {'regions': [], 'error': str(e)}
    
    async def _understand_state(self, frame: np.ndarray, spatial: Dict[str, Any], 
                               context: Optional[Dict[str, Any]], metrics: ProcessingMetrics) -> Dict[str, Any]:
        """Stage 3: State understanding using VSMS"""
        if self.system_mode == SystemMode.EMERGENCY:
            return {'state': 'unknown', 'skipped': True}
        
        # Lazy load VSMS
        if self.components['vsms'] is None:
            try:
                from .vsms_core import get_vsms
                self.components['vsms'] = get_vsms()
            except Exception as e:
                logger.warning(f"Could not load VSMS: {e}")
                return {'state': 'unknown', 'error': str(e)}
        
        try:
            vsms = self.components['vsms']
            state_result = await vsms.process_visual_observation(
                screenshot=frame,
                regions=spatial.get('regions', []),
                context=context
            )
            
            return {
                'state': state_result.get('state', 'unknown'),
                'confidence': state_result.get('confidence', 0.0),
                'scene_graph': state_result.get('scene_graph', {}),
                'temporal_context': state_result.get('temporal_context', {})
            }
        except Exception as e:
            logger.error(f"State understanding failed: {e}")
            return {'state': 'unknown', 'error': str(e)}
    
    async def _process_intelligence(self, state: Dict[str, Any], metrics: ProcessingMetrics) -> Dict[str, Any]:
        """Stage 4: Intelligence processing"""
        if self.system_mode in [SystemMode.CRITICAL, SystemMode.EMERGENCY]:
            return {'intelligence': 'limited', 'skipped': True}
        
        intelligence_result = {
            'activity': None,
            'goals': [],
            'patterns': [],
            'anomalies': []
        }
        
        # Activity recognition
        if self.components['activity_recognition'] is None and self.system_mode == SystemMode.NORMAL:
            try:
                from .activity_recognition_engine import get_activity_recognizer
                self.components['activity_recognition'] = get_activity_recognizer()
            except Exception:
                pass

        if self.components['activity_recognition']:
            try:
                activity = await self.components['activity_recognition'].recognize(state)
                intelligence_result['activity'] = activity
            except Exception:
                pass

        # Goal inference
        if self.components['goal_inference'] is None and self.system_mode == SystemMode.NORMAL:
            try:
                from .goal_inference_system import get_goal_inference
                self.components['goal_inference'] = get_goal_inference()
            except Exception:
                pass

        if self.components['goal_inference']:
            try:
                goals = await self.components['goal_inference'].infer_goals(state)
                intelligence_result['goals'] = goals
            except Exception:
                pass
        
        return intelligence_result
    
    async def _check_caches(self, frame: np.ndarray, intelligence: Dict[str, Any], 
                          metrics: ProcessingMetrics) -> Optional[Dict[str, Any]]:
        """Stage 5: Check caches"""
        # Bloom filter check
        if self.components['bloom_filter'] is None:
            try:
                from vision.bloom_filter_network import get_bloom_filter_network
                self.components['bloom_filter'] = get_bloom_filter_network()
            except Exception:
                pass

        # Generate cache key
        cache_key = self._generate_cache_key(frame, intelligence)

        # Check bloom filter first
        if self.components['bloom_filter']:
            if self.components['bloom_filter'].check_duplicate(cache_key):
                # Check semantic cache
                if self.components['semantic_cache'] is None:
                    try:
                        from .semantic_cache_lsh import get_semantic_cache
                        self.components['semantic_cache'] = await get_semantic_cache()
                    except Exception:
                        pass

                if self.components['semantic_cache']:
                    result = await self.components['semantic_cache'].get(cache_key)
                    if result:
                        return result[0]  # Return cached value

        return None

    async def _generate_predictions(self, state: Dict[str, Any], intelligence: Dict[str, Any],
                                  metrics: ProcessingMetrics) -> Optional[Dict[str, Any]]:
        """Stage 6: Generate predictions"""
        if self.system_mode in [SystemMode.CRITICAL, SystemMode.EMERGENCY]:
            return None

        if self.components['predictive_engine'] is None:
            try:
                from .predictive_precomputation_engine import get_predictive_engine
                self.components['predictive_engine'] = await get_predictive_engine()
            except Exception:
                return None

        try:
            prediction = await self.components['predictive_engine'].predict(state, intelligence)
            return prediction
        except Exception:
            return None
    
    async def _make_api_decision(self, frame: np.ndarray, spatial: Dict[str, Any],
                               intelligence: Dict[str, Any], metrics: ProcessingMetrics) -> Dict[str, Any]:
        """Stage 7: Make API decision"""
        # This would integrate with the actual API caller
        # For now, return a placeholder
        return {
            'api_called': True,
            'regions_sent': len(spatial.get('regions', [])),
            'compression_applied': self.system_mode != SystemMode.NORMAL
        }
    
    async def _integrate_response(self, api_result: Dict[str, Any], state: Dict[str, Any],
                                intelligence: Dict[str, Any], metrics: ProcessingMetrics) -> Dict[str, Any]:
        """Stage 8: Integrate response"""
        integrated = {
            **api_result,
            'state': state,
            'intelligence': intelligence
        }
        
        # Update caches
        if self.components['semantic_cache']:
            cache_key = self._generate_cache_key(None, intelligence)
            await self.components['semantic_cache'].put(cache_key, integrated)
        
        # Update predictive engine
        if self.components['predictive_engine']:
            await self.components['predictive_engine'].update_state(state)
        
        return integrated
    
    async def _apply_proactive_intelligence(self, result: Dict[str, Any], 
                                          metrics: ProcessingMetrics) -> Dict[str, Any]:
        """Stage 9: Apply proactive intelligence"""
        if self.system_mode in [SystemMode.CRITICAL, SystemMode.EMERGENCY]:
            return result
        
        # Anomaly detection
        if self.components['anomaly_detection'] is None:
            try:
                from .multi_modal_anomaly_detector import get_anomaly_detector
                self.components['anomaly_detection'] = get_anomaly_detector()
            except Exception:
                pass

        if self.components['anomaly_detection']:
            try:
                anomalies = await self.components['anomaly_detection'].detect(result)
                result['anomalies'] = anomalies

                # Intervention if needed
                if anomalies and self.components['intervention_engine'] is None:
                    try:
                        from .intelligent_intervention_engine import get_intervention_engine
                        self.components['intervention_engine'] = get_intervention_engine()
                    except Exception:
                        pass

                if anomalies and self.components['intervention_engine']:
                    interventions = await self.components['intervention_engine'].decide(anomalies)
                    result['interventions'] = interventions
            except Exception:
                pass
        
        return result
    
    def _generate_cache_key(self, frame: Optional[np.ndarray], intelligence: Dict[str, Any]) -> str:
        """Generate cache key from frame and intelligence data"""
        import hashlib
        key_parts = []
        
        if frame is not None:
            # Use frame shape and sample pixels for key
            key_parts.append(f"shape_{frame.shape}")
            sample_pixels = frame[::100, ::100].flatten()[:100]
            key_parts.append(f"pixels_{hash(sample_pixels.tobytes())}")
        
        # Add intelligence data
        key_parts.append(f"activity_{intelligence.get('activity', 'none')}")
        key_parts.append(f"goals_{len(intelligence.get('goals', []))}")
        
        return hashlib.sha256('_'.join(key_parts).encode()).hexdigest()
    
    def _build_result(self, data: Dict[str, Any], metrics: ProcessingMetrics) -> Dict[str, Any]:
        """Build final result with metrics"""
        return {
            **data,
            '_metrics': {
                'total_time': metrics.total_time,
                'stage_times': metrics.stage_times,
                'cache_hits': metrics.cache_hits,
                'api_calls_saved': metrics.api_calls_saved,
                'predictions_used': metrics.predictions_used,
                'system_mode': metrics.system_mode.value,
                'memory_usage_mb': sum(metrics.memory_usage.values())
            }
        }
    
    async def get_system_status(self) -> Dict[str, Any]:
        """Get current system status"""
        process = psutil.Process()
        memory_info = process.memory_info()
        
        component_status = {}
        for name, component in self.components.items():
            if component is None:
                component_status[name] = 'disabled'
            else:
                component_status[name] = 'active'
        
        allocation_status = {}
        for name, alloc in self.memory_allocations.items():
            allocation_status[name] = {
                'allocated_mb': alloc.allocated_mb,
                'used_mb': alloc.used_mb,
                'utilization': alloc.used_mb / alloc.allocated_mb if alloc.allocated_mb > 0 else 0
            }
        
        return {
            'system_mode': self.system_mode.value,
            'memory_usage_mb': memory_info.rss / 1024 / 1024,
            'memory_percent': process.memory_percent(),
            'components': component_status,
            'allocations': allocation_status,
            'queue_size': self.processing_queue.qsize()
        }


# Singleton instance
_orchestrator_instance: Optional[IntegrationOrchestrator] = None


def get_integration_orchestrator(config: Optional[Dict[str, Any]] = None) -> IntegrationOrchestrator:
    """Get singleton orchestrator instance"""
    global _orchestrator_instance
    if _orchestrator_instance is None:
        _orchestrator_instance = IntegrationOrchestrator(config)
    return _orchestrator_instance