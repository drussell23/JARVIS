#!/usr/bin/env python3
"""
Intelligent ML Memory Manager for JARVIS
========================================

Advanced memory management system that:
1. Monitors memory usage in real-time
2. Loads/unloads models based on context
3. Uses Rust for performance-critical operations
4. Implements model quantization
5. Respects 16GB RAM constraints (target: 35% usage)

Memory Budget Allocation (5.6GB total for 35% of 16GB):
- System/OS: 2GB
- JARVIS Core: 1GB 
- ML Models: 2GB (dynamic allocation)
- Buffer: 0.6GB
"""

import os
import gc
import psutil
import time
import json
import threading
import weakref
from pathlib import Path
from typing import Dict, Any, Optional, List, Set, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections import OrderedDict
from enum import Enum
import logging
import asyncio
import numpy as np

# Import enhanced logging
try:
    from ml_logging_config import ml_logger, memory_visualizer
    ENHANCED_LOGGING = True
except ImportError:
    ENHANCED_LOGGING = False
    
logger = logging.getLogger(__name__)

# Try to import Rust extension for performance
try:
    # Try direct import first
    import jarvis_rust_extensions
    RustMemoryMonitor = jarvis_rust_extensions.RustMemoryMonitor
    RustModelLoader = jarvis_rust_extensions.RustModelLoader
    compress_data_lz4 = jarvis_rust_extensions.compress_data_lz4
    decompress_data_lz4 = jarvis_rust_extensions.decompress_data_lz4
    RUST_AVAILABLE = True
    logger.info("Rust extensions loaded successfully")
except ImportError:
    try:
        # Try from module
        from .rust_extensions import RustMemoryMonitor, RustModelLoader
        RUST_AVAILABLE = True
    except ImportError:
        RUST_AVAILABLE = False
        logger.warning("Rust extensions not available - using Python fallback")


class ModelPriority(Enum):
    """Model priority levels"""
    CRITICAL = 1      # Always loaded (core functionality)
    HIGH = 2          # Load when memory available
    MEDIUM = 3        # Load on demand
    LOW = 4           # Load only when explicitly needed
    PREEMPTIBLE = 5   # Can be unloaded anytime


@dataclass
class ModelConfig:
    """Configuration for each ML model"""
    name: str
    path: Optional[str]
    size_mb: float
    priority: ModelPriority
    load_time_ms: int  # Expected load time
    ttl_seconds: int = 300  # Time to live after last use
    quantizable: bool = True
    context_triggers: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    
    
@dataclass
class ModelState:
    """Runtime state of a model"""
    config: ModelConfig
    loaded: bool = False
    load_timestamp: Optional[datetime] = None
    last_used: Optional[datetime] = None
    use_count: int = 0
    memory_usage_mb: float = 0.0
    model_ref: Optional[weakref.ref] = None  # Weak reference to allow GC


class ContextAwareModelLoader:
    """Context-aware ML model loading system"""
    
    def __init__(self):
        self.current_context: Set[str] = set()
        self.context_history: List[Tuple[datetime, Set[str]]] = []
        self.predictions: Dict[str, float] = {}  # Context -> probability
        
    def update_context(self, context_items: List[str]):
        """Update current context"""
        self.current_context = set(context_items)
        self.context_history.append((datetime.now(), self.current_context.copy()))
        
        # Keep only last 100 context updates
        if len(self.context_history) > 100:
            self.context_history.pop(0)
            
    def predict_needed_models(self, all_models: Dict[str, ModelConfig]) -> List[str]:
        """Predict which models will be needed based on context"""
        needed = []
        
        for model_name, config in all_models.items():
            # Check if any context triggers match
            if any(trigger in self.current_context for trigger in config.context_triggers):
                needed.append(model_name)
                
        # Add predictions based on history
        for context in self.current_context:
            # Simple frequency-based prediction
            count = sum(1 for _, hist_ctx in self.context_history if context in hist_ctx)
            probability = count / max(len(self.context_history), 1)
            
            if probability > 0.3:  # 30% threshold
                # Find models associated with this context
                for model_name, config in all_models.items():
                    if context in config.context_triggers and model_name not in needed:
                        needed.append(model_name)
                        
        return needed


class IntelligentMLMemoryManager:
    """Main ML memory management system"""
    
    # Model configurations
    MODEL_CONFIGS = {
        # Voice models
        "whisper_base": ModelConfig(
            name="whisper_base",
            path="openai/whisper-base",
            size_mb=150,
            priority=ModelPriority.HIGH,
            load_time_ms=2000,
            context_triggers=["voice", "speech", "audio", "transcribe"]
        ),
        "voice_biometric": ModelConfig(
            name="voice_biometric",
            path="voice_unlock/models/biometric.pkl",
            size_mb=50,
            priority=ModelPriority.MEDIUM,
            load_time_ms=500,
            context_triggers=["unlock", "authenticate", "security"],
            ttl_seconds=30  # Unload quickly after use
        ),
        
        # Vision models
        "vision_encoder": ModelConfig(
            name="vision_encoder",
            path="vision/models/encoder.onnx",
            size_mb=200,
            priority=ModelPriority.LOW,
            load_time_ms=3000,
            context_triggers=["vision", "screen", "monitor", "analyze"],
            quantizable=True
        ),
        
        # NLP models
        "embeddings": ModelConfig(
            name="embeddings",
            path="sentence-transformers/all-MiniLM-L6-v2",
            size_mb=100,
            priority=ModelPriority.CRITICAL,
            load_time_ms=1000,
            context_triggers=["search", "context", "understand"]
        ),
        "sentiment": ModelConfig(
            name="sentiment",
            path="nlp/sentiment.pkl",
            size_mb=30,
            priority=ModelPriority.LOW,
            load_time_ms=300,
            context_triggers=["emotion", "sentiment", "mood"]
        )
    }
    
    def __init__(self, memory_budget_mb: int = 2048):  # 2GB for ML models
        self.memory_budget_mb = memory_budget_mb
        self.models: Dict[str, ModelState] = {}
        self.context_loader = ContextAwareModelLoader()
        self.lock = threading.Lock()
        
        # Initialize Rust components if available
        if RUST_AVAILABLE:
            self.rust_monitor = RustMemoryMonitor()
            self.rust_loader = RustModelLoader()
        else:
            self.rust_monitor = None
            self.rust_loader = None
            
        # Background tasks
        self._stop_event = threading.Event()
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        
        # Initialize model states
        for name, config in self.MODEL_CONFIGS.items():
            self.models[name] = ModelState(config=config)
            
        # Start background tasks
        self._monitor_thread.start()
        self._cleanup_thread.start()
        
        logger.info(f"ML Memory Manager initialized with {memory_budget_mb}MB budget")
        
    def get_memory_usage(self) -> Dict[str, Any]:
        """Get current memory usage statistics"""
        if self.rust_monitor:
            # Use Rust for fast memory monitoring
            return self.rust_monitor.get_memory_stats()
            
        # Python fallback
        memory = psutil.virtual_memory()
        ml_memory = sum(state.memory_usage_mb for state in self.models.values() if state.loaded)
        
        return {
            "total_mb": memory.total / 1024 / 1024,
            "available_mb": memory.available / 1024 / 1024,
            "ml_models_mb": ml_memory,
            "ml_models_count": sum(1 for s in self.models.values() if s.loaded),
            "system_percent": memory.percent,
            "ml_percent": (ml_memory / (memory.total / 1024 / 1024)) * 100
        }
        
    async def load_model(self, model_name: str, force: bool = False) -> Optional[Any]:
        """Load a model with intelligent memory management"""
        if model_name not in self.models:
            logger.error(f"Unknown model: {model_name}")
            return None
            
        # Enhanced logging
        if ENHANCED_LOGGING:
            config = self.MODEL_CONFIGS.get(model_name)
            if config:
                ml_logger.load_start(model_name, config.size_mb, 
                                   f"Context: {', '.join(self.context_loader.current_context)}")
            
        state = self.models[model_name]
        
        # Check if already loaded
        if state.loaded and state.model_ref:
            model = state.model_ref()
            if model is not None:
                state.last_used = datetime.now()
                state.use_count += 1
                if ENHANCED_LOGGING:
                    ml_logger.cache_hit(model_name)
                return model
                
        # Check memory availability
        if not force and not self._can_load_model(state.config):
            memory_stats = self.get_memory_usage()
            if ENHANCED_LOGGING:
                ml_logger.memory_check(memory_stats, f"Cannot load {model_name} - insufficient memory")
                ml_logger.cleanup_triggered("Memory pressure", len([s for s in self.models.values() if s.loaded]))
            logger.warning(f"Insufficient memory to load {model_name}")
            # Try to free memory
            freed = await self._free_memory_for_model(state.config)
            if not freed:
                if ENHANCED_LOGGING:
                    ml_logger.load_failed(model_name, "Insufficient memory after cleanup", memory_stats)
                return None
                
        # Load the model
        start_time = time.time()
        try:
            if self.rust_loader and state.config.quantizable:
                # Use Rust for quantized loading
                model = await self._load_with_rust(state.config)
            else:
                # Python loading
                model = await self._load_with_python(state.config)
                
            # Update state
            with self.lock:
                state.loaded = True
                state.load_timestamp = datetime.now()
                state.last_used = datetime.now()
                state.model_ref = weakref.ref(model, lambda ref: self._on_model_gc(model_name))
                state.memory_usage_mb = self._estimate_model_memory(model)
                
            logger.info(f"Loaded {model_name} ({state.memory_usage_mb:.1f}MB)")
            
            # Enhanced logging with memory info
            if ENHANCED_LOGGING:
                load_time = time.time() - start_time
                memory_stats = self.get_memory_usage()
                ml_logger.load_success(model_name, load_time, memory_stats)
                
                # Visualize memory if enabled
                loaded_models = {
                    name: {
                        'size_mb': state.memory_usage_mb,
                        'last_used_s': 0,
                        'quantized': state.config.quantizable
                    }
                    for name, state in self.models.items() if state.loaded
                }
                memory_visualizer.visualize_memory(memory_stats, loaded_models)
                
            return model
            
        except Exception as e:
            logger.error(f"Failed to load {model_name}: {e}")
            if ENHANCED_LOGGING:
                ml_logger.load_failed(model_name, str(e), self.get_memory_usage())
            return None
            
    async def unload_model(self, model_name: str) -> bool:
        """Unload a model to free memory"""
        if model_name not in self.models:
            return False
            
        state = self.models[model_name]
        if not state.loaded:
            return True
            
        # Enhanced logging
        if ENHANCED_LOGGING:
            ttl_expired = False
            if state.last_used:
                age = (datetime.now() - state.last_used).total_seconds()
                ttl_expired = age > state.config.ttl_seconds
            reason = "TTL expired" if ttl_expired else "Memory pressure"
            ml_logger.unload(model_name, state.memory_usage_mb, reason)
            
        with self.lock:
            state.loaded = False
            state.model_ref = None
            memory_freed = state.memory_usage_mb
            state.memory_usage_mb = 0.0
            
        # Force garbage collection
        gc.collect()
        
        logger.info(f"Unloaded {model_name}, freed ~{memory_freed:.1f}MB")
        
        # Update visualization
        if ENHANCED_LOGGING:
            memory_stats = self.get_memory_usage()
            loaded_models = {
                name: {
                    'size_mb': s.memory_usage_mb,
                    'last_used_s': (datetime.now() - s.last_used).total_seconds() if s.last_used else 0,
                    'quantized': s.config.quantizable
                }
                for name, s in self.models.items() if s.loaded
            }
            memory_visualizer.visualize_memory(memory_stats, loaded_models)
            
        return True
        
    def update_context(self, context: List[str]):
        """Update context for intelligent preloading"""
        self.context_loader.update_context(context)
        
        # Asynchronously preload predicted models
        asyncio.create_task(self._preload_predicted_models())
        
    async def _preload_predicted_models(self):
        """Preload models based on context prediction"""
        predicted = self.context_loader.predict_needed_models(self.MODEL_CONFIGS)
        
        if ENHANCED_LOGGING and predicted:
            ml_logger.prediction(predicted, 80.0)  # 80% confidence placeholder
            
        for model_name in predicted:
            state = self.models[model_name]
            if not state.loaded and state.config.priority != ModelPriority.PREEMPTIBLE:
                # Preload in background
                asyncio.create_task(self.load_model(model_name))
                
    def _can_load_model(self, config: ModelConfig) -> bool:
        """Check if model can be loaded within memory budget.

        .. deprecated::
            Use :func:`MemoryBudgetBroker.try_request` instead.
        """
        # Memory Control Plane: delegate to broker if available
        try:
            import warnings
            warnings.warn(
                "IntelligentMLMemoryManager._can_load_model() is deprecated. "
                "Use MemoryBudgetBroker.try_request() instead.",
                DeprecationWarning,
                stacklevel=2,
            )
        except Exception:
            pass
        current_usage = self.get_memory_usage()
        available_mb = min(
            current_usage["available_mb"],
            self.memory_budget_mb - current_usage["ml_models_mb"]
        )
        
        # Add 20% buffer for safety
        required_mb = config.size_mb * 1.2
        
        return available_mb >= required_mb
        
    async def _free_memory_for_model(self, config: ModelConfig) -> bool:
        """Try to free memory for a model"""
        required_mb = config.size_mb * 1.2
        freed_mb = 0.0
        
        # Sort models by priority and last used time
        loaded_models = [
            (name, state) for name, state in self.models.items()
            if state.loaded and state.config.priority.value > config.priority.value
        ]
        loaded_models.sort(
            key=lambda x: (x[1].config.priority.value, x[1].last_used or datetime.min)
        )
        
        # Unload models until we have enough memory
        for model_name, state in loaded_models:
            if freed_mb >= required_mb:
                break
                
            if await self.unload_model(model_name):
                freed_mb += state.memory_usage_mb
                
        return freed_mb >= required_mb
        
    async def _load_with_rust(self, config: ModelConfig) -> Any:
        """Load model using Rust for performance"""
        if not self.rust_loader:
            return await self._load_with_python(config)
            
        try:
            # Use Rust for quantized loading
            if config.path.endswith('.pkl'):
                # Load with quantization
                quantized_data = self.rust_loader.load_quantized_model(
                    config.path, 
                    quantize_to_int8=True
                )
                
                # Decompress and load
                import pickle
                decompressed = self.rust_loader.decompress_model(quantized_data)
                return pickle.loads(decompressed)
                
            elif config.path.endswith('.onnx'):
                # Memory-mapped loading for ONNX
                return self.rust_loader.mmap_model(config.path)
                
            else:
                # Fallback to Python
                return await self._load_with_python(config)
                
        except Exception as e:
            logger.warning(f"Rust loading failed, falling back to Python: {e}")
            return await self._load_with_python(config)
        
    async def _load_with_python(self, config: ModelConfig) -> Any:
        """Load model using Python"""
        if "whisper" in config.name:
            import whisper
            return whisper.load_model(config.path.split("-")[-1])
            
        elif "sentence-transformers" in config.path:
            from sentence_transformers import SentenceTransformer
            return SentenceTransformer(config.path)
            
        elif config.path.endswith(".pkl"):
            import pickle
            with open(config.path, "rb") as f:
                return pickle.load(f)
                
        elif config.path.endswith(".onnx"):
            import onnxruntime as ort
            return ort.InferenceSession(config.path)
            
        else:
            raise ValueError(f"Unknown model type: {config.path}")
            
    def _estimate_model_memory(self, model: Any) -> float:
        """Estimate model memory usage"""
        if self.rust_monitor:
            # Use Rust for accurate memory measurement
            stats_before = self.rust_monitor.get_memory_stats()
            import sys
            size_bytes = sys.getsizeof(model)
            
            # For numpy arrays, get actual memory usage
            if hasattr(model, 'nbytes'):
                size_bytes = model.nbytes
            elif hasattr(model, '__sizeof__'):
                size_bytes = model.__sizeof__()
                
            # Update Rust monitor with ML memory usage
            current_ml_mb = stats_before['ml_models_mb'] + (size_bytes / 1024 / 1024)
            current_ml_count = stats_before['ml_models_count'] + 1
            self.rust_monitor.update_ml_stats(current_ml_mb, current_ml_count)
            
            return size_bytes / 1024 / 1024
        else:
            # Simple estimation
            import sys
            return sys.getsizeof(model) / 1024 / 1024
        
    def _on_model_gc(self, model_name: str):
        """Callback when model is garbage collected"""
        logger.debug(f"Model {model_name} was garbage collected")
        if model_name in self.models:
            self.models[model_name].loaded = False
            
    def _monitor_loop(self):
        """Background monitoring loop"""
        # Create persistent event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            while not self._stop_event.is_set():
                try:
                    stats = self.get_memory_usage()

                    # Critical memory pressure
                    if stats["system_percent"] > 70:
                        logger.warning(f"High memory pressure: {stats['system_percent']:.1f}%")
                        if ENHANCED_LOGGING:
                            ml_logger.critical_memory(stats['system_percent'], "Emergency unloading low priority models")
                        # Unload low priority models
                        for name, state in self.models.items():
                            if (state.loaded and
                                state.config.priority == ModelPriority.PREEMPTIBLE):
                                loop.run_until_complete(self.unload_model(name))

                except Exception as e:
                    logger.error(f"Monitor error: {e}")

                self._stop_event.wait(5)  # Check every 5 seconds
        finally:
            # Clean up event loop
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.close()
            except Exception as e:
                logger.debug(f"Monitor loop cleanup error: {e}")
            
    def _cleanup_loop(self):
        """Background cleanup loop"""
        # Create persistent event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            while not self._stop_event.is_set():
                try:
                    now = datetime.now()

                    # Check TTL for loaded models
                    for name, state in self.models.items():
                        if not state.loaded:
                            continue

                        if state.last_used:
                            age = (now - state.last_used).total_seconds()
                            if age > state.config.ttl_seconds:
                                logger.info(f"Model {name} TTL expired, unloading")
                                loop.run_until_complete(self.unload_model(name))

                except Exception as e:
                    logger.error(f"Cleanup error: {e}")

                self._stop_event.wait(30)  # Check every 30 seconds
        finally:
            # Clean up event loop
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.close()
            except Exception as e:
                logger.debug(f"Cleanup loop cleanup error: {e}")
            
    def shutdown(self):
        """Shutdown the memory manager"""
        logger.info("Shutting down ML Memory Manager")
        self._stop_event.set()

        # Wait for monitor thread
        if self._monitor_thread and self._monitor_thread.is_alive():
            logger.info("   Waiting for monitor thread...")
            self._monitor_thread.join(timeout=5)
            if self._monitor_thread.is_alive():
                logger.warning("   Monitor thread did not exit cleanly")
                self._monitor_thread.daemon = True
            self._monitor_thread = None

        # Wait for cleanup thread
        if self._cleanup_thread and self._cleanup_thread.is_alive():
            logger.info("   Waiting for cleanup thread...")
            self._cleanup_thread.join(timeout=5)
            if self._cleanup_thread.is_alive():
                logger.warning("   Cleanup thread did not exit cleanly")
                self._cleanup_thread.daemon = True
            self._cleanup_thread = None

        # Unload all models using existing event loop or creating new one
        if self.models:
            logger.info(f"   Unloading {len(self.models)} models...")
            try:
                # Try to get existing event loop
                loop = asyncio.get_event_loop()
                if loop.is_closed():
                    raise RuntimeError("Loop is closed")
            except RuntimeError:
                # Create new loop if needed
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                needs_close = True
            else:
                needs_close = False

            try:
                for name in list(self.models.keys()):
                    loop.run_until_complete(self.unload_model(name))
            finally:
                if needs_close:
                    loop.close()

        logger.info("✅ ML Memory Manager shutdown complete")
            

# Global instance
_ml_memory_manager = None


def get_ml_memory_manager() -> IntelligentMLMemoryManager:
    """Get or create the global ML memory manager"""
    global _ml_memory_manager
    if _ml_memory_manager is None:
        _ml_memory_manager = IntelligentMLMemoryManager()
    return _ml_memory_manager


async def load_model_for_context(model_name: str, context: List[str]) -> Optional[Any]:
    """Load a model with context awareness"""
    manager = get_ml_memory_manager()
    manager.update_context(context)
    return await manager.load_model(model_name)


def get_ml_memory_stats() -> Dict[str, Any]:
    """Get current ML memory statistics"""
    manager = get_ml_memory_manager()
    return manager.get_memory_usage()