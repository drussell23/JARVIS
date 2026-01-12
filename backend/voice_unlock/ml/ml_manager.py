"""
ML Model Manager for Voice Unlock
=================================

Lightweight ML management system optimized for 16GB RAM constraints.
Features:
- Dynamic model loading/unloading
- Memory-efficient caching
- Model quantization
- Performance monitoring
"""

import os
import gc
import psutil
import threading
import time
import joblib
import pickle
import numpy as np
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, field
from collections import OrderedDict
import logging
from datetime import datetime, timedelta
import json
import tempfile
import mmap

# For model quantization
try:
    import onnx
    import onnxruntime as ort
    ONNX_AVAILABLE = True
except ImportError:
    ONNX_AVAILABLE = False

# For model compression
try:
    import tensorflow as tf
    import tensorflow_model_optimization as tfmot
    TF_OPTIMIZATION_AVAILABLE = True
except ImportError:
    TF_OPTIMIZATION_AVAILABLE = False

from sklearn.preprocessing import StandardScaler
from sklearn.svm import OneClassSVM

# Setup logger early (needed for import error handling)
logger = logging.getLogger(__name__)

# Import resource manager for strict control
try:
    from ...resource_manager import get_resource_manager, throttled_operation
    RESOURCE_MANAGER_AVAILABLE = True
except ImportError:
    RESOURCE_MANAGER_AVAILABLE = False
    logger.warning("Resource manager not available - running without strict limits")


@dataclass
class ModelMetadata:
    """Metadata for loaded models"""
    model_id: str
    model_type: str
    size_bytes: int
    load_time: datetime
    last_access: datetime
    access_count: int = 0
    quantized: bool = False
    compressed: bool = False
    memory_mapped: bool = False
    path: Optional[Path] = None


@dataclass
class MemoryStats:
    """Memory usage statistics"""
    total_ram: int
    available_ram: int
    used_ram: int
    percent_used: float
    ml_memory_used: int
    model_count: int
    cache_hits: int = 0
    cache_misses: int = 0


class ModelCache:
    """LRU cache for ML models with memory limits"""
    
    def __init__(self, max_memory_mb: int = 200):
        self.max_memory = max_memory_mb * 1024 * 1024  # Convert to bytes
        self.cache: OrderedDict[str, Tuple[Any, ModelMetadata]] = OrderedDict()
        self.current_memory = 0
        self.lock = threading.Lock()
        
    def get(self, key: str) -> Optional[Any]:
        """Get model from cache"""
        with self.lock:
            if key in self.cache:
                # Move to end (most recently used)
                model, metadata = self.cache.pop(key)
                metadata.last_access = datetime.now()
                metadata.access_count += 1
                self.cache[key] = (model, metadata)
                return model
            return None
            
    def put(self, key: str, model: Any, metadata: ModelMetadata):
        """Add model to cache with eviction if needed"""
        with self.lock:
            model_size = metadata.size_bytes
            
            # Evict models if needed
            while self.current_memory + model_size > self.max_memory and self.cache:
                # Remove least recently used
                evicted_key, (evicted_model, evicted_meta) = self.cache.popitem(last=False)
                self.current_memory -= evicted_meta.size_bytes
                logger.info(f"Evicted model {evicted_key} from cache")
                
            # Add new model
            self.cache[key] = (model, metadata)
            self.current_memory += model_size
            
    def clear(self):
        """Clear all cached models"""
        with self.lock:
            self.cache.clear()
            self.current_memory = 0
            gc.collect()


class MLModelManager:
    """
    Manages ML models with memory optimization for 16GB RAM systems
    """
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        # Merge provided config with defaults
        default_config = self._get_default_config()
        if config:
            default_config.update(config)
        self.config = default_config
        self.models: Dict[str, Tuple[Any, ModelMetadata]] = {}
        self.cache = ModelCache(self.config['max_cache_memory_mb'])
        self.lock = threading.Lock()
        
        # Lazy loading components
        self.model_loaders: Dict[str, callable] = {}
        self.pending_loads: Dict[str, threading.Event] = {}
        self.preload_queue: OrderedDict[str, float] = OrderedDict()  # model_id -> priority
        
        # Model access patterns for predictive loading
        self.access_history: OrderedDict[str, List[datetime]] = OrderedDict()
        self.access_predictions: Dict[str, float] = {}  # model_id -> probability of next access
        
        # Performance monitoring
        self.stats = {
            'models_loaded': 0,
            'models_unloaded': 0,
            'total_load_time': 0.0,
            'memory_saved_mb': 0.0,
            'lazy_loads': 0,
            'preemptive_loads': 0
        }
        
        # Memory monitoring thread
        self.monitor_thread = None
        self.monitoring = False
        
        # Preload thread for predictive loading
        self.preload_thread = None
        self.preloading = False
        
        if self.config['enable_monitoring']:
            self._start_monitoring()
            
        if self.config['enable_predictive_loading']:
            self._start_preloading()
            
        # Register with resource manager
        if RESOURCE_MANAGER_AVAILABLE:
            rm = get_resource_manager()
            rm.register_unload_callback('ml_model', self._emergency_unload_all)
            rm.register_throttle_callback(self._handle_throttle_change)
            logger.info("Registered with resource manager for strict control")
            
    def _get_default_config(self) -> Dict[str, Any]:
        """Get default configuration"""
        # Ultra-aggressive settings for 30% memory target
        return {
            'max_memory_mb': 300,  # Reduced from 500 - Max memory for ML models
            'max_cache_memory_mb': 100,  # Reduced from 200 - Max cache size
            'enable_quantization': True,
            'enable_compression': True,
            'enable_mmap': True,  # Memory-mapped files
            'enable_monitoring': True,
            'enable_predictive_loading': True,  # Predictive model loading
            'enable_lazy_loading': True,  # Extreme lazy loading
            'enable_ultra_optimization': True,  # New: Ultra mode for 30% target
            'monitoring_interval': 2.0,  # Reduced from 5.0 - More frequent checks
            'auto_unload_timeout': 60,  # Reduced from 300 - 1 minute
            'aggressive_unload_timeout': 30,  # Reduced from 60 - 30 seconds
            'panic_unload_timeout': 10,  # New: 10 seconds in panic mode
            'quantization_bits': 8,  # INT8 quantization (consider INT4 for ultra mode)
            'compression_ratio': 0.3,  # More aggressive compression
            'preload_threshold': 0.9,  # Only preload if >90% certain (was 0.7)
            'max_preload_queue': 1,  # Only 1 model in queue (was 3)
            'load_timeout': 10.0  # Max seconds to wait for model load
        }
        
    @throttled_operation if RESOURCE_MANAGER_AVAILABLE else lambda f: f
    def load_model(self, model_id: str, model_path: Path, 
                   model_type: str = 'sklearn') -> Any:
        """
        Load model with memory optimization and resource management
        
        Args:
            model_id: Unique identifier for the model
            model_path: Path to model file
            model_type: Type of model (sklearn, onnx, tensorflow)
        
        Returns:
            Loaded model object
        """
        # Check with resource manager first
        if RESOURCE_MANAGER_AVAILABLE:
            rm = get_resource_manager()
            if not rm.request_ml_model(model_id):
                raise MemoryError(f"Resource manager denied loading model {model_id} due to memory pressure")
        
        start_time = time.time()
        
        # Check cache first
        cached_model = self.cache.get(model_id)
        if cached_model is not None:
            self.stats['cache_hits'] = self.stats.get('cache_hits', 0) + 1
            logger.info(f"Model {model_id} loaded from cache")
            return cached_model
            
        self.stats['cache_misses'] = self.stats.get('cache_misses', 0) + 1
        
        with self.lock:
            # Check if already loaded
            if model_id in self.models:
                model, metadata = self.models[model_id]
                metadata.last_access = datetime.now()
                metadata.access_count += 1
                return model
                
            # Load based on type
            if model_type == 'sklearn':
                model = self._load_sklearn_model(model_path)
            elif model_type == 'onnx' and ONNX_AVAILABLE:
                model = self._load_onnx_model(model_path)
            elif model_type == 'tensorflow' and TF_OPTIMIZATION_AVAILABLE:
                model = self._load_tensorflow_model(model_path)
            else:
                raise ValueError(f"Unsupported model type: {model_type}")
                
            # Get model size
            model_size = self._get_model_size(model)
            
            # Apply optimizations
            if self.config['enable_quantization']:
                model = self._quantize_model(model, model_type)
                
            if self.config['enable_compression']:
                model = self._compress_model(model, model_type)
                
            # Create metadata
            metadata = ModelMetadata(
                model_id=model_id,
                model_type=model_type,
                size_bytes=model_size,
                load_time=datetime.now(),
                last_access=datetime.now(),
                quantized=self.config['enable_quantization'],
                compressed=self.config['enable_compression'],
                path=model_path
            )
            
            # Store model
            self.models[model_id] = (model, metadata)
            
            # Add to cache
            self.cache.put(model_id, model, metadata)
            
            # Update stats
            load_time = time.time() - start_time
            self.stats['models_loaded'] += 1
            self.stats['total_load_time'] += load_time
            
            logger.info(f"Model {model_id} loaded in {load_time:.2f}s, size: {model_size/1024/1024:.2f}MB")
            
            return model
            
    def register_lazy_loader(self, model_id: str, loader_func: callable, 
                           model_path: Path, model_type: str = 'sklearn'):
        """
        Register a lazy loader for a model without loading it
        
        Args:
            model_id: Unique identifier for the model
            loader_func: Function to call when model needs to be loaded
            model_path: Path to model file
            model_type: Type of model
        """
        self.model_loaders[model_id] = {
            'loader': loader_func,
            'path': model_path,
            'type': model_type,
            'registered_at': datetime.now()
        }
        
        # Initialize access tracking
        if model_id not in self.access_history:
            self.access_history[model_id] = []
            
        logger.debug(f"Registered lazy loader for {model_id}")
        
    def get_model_lazy(self, model_id: str, timeout: Optional[float] = None) -> Optional[Any]:
        """
        Get model with extreme lazy loading
        
        Args:
            model_id: Model identifier
            timeout: Max seconds to wait for model load
            
        Returns:
            Model object or None if not available
        """
        timeout = timeout or self.config['load_timeout']
        
        # Track access for prediction
        self._track_access(model_id)
        
        # Check if already loaded
        cached_model = self.cache.get(model_id)
        if cached_model is not None:
            return cached_model
            
        with self.lock:
            if model_id in self.models:
                model, metadata = self.models[model_id]
                metadata.last_access = datetime.now()
                metadata.access_count += 1
                return model
                
        # Check if loader is registered
        if model_id not in self.model_loaders:
            logger.warning(f"No loader registered for {model_id}")
            return None
            
        # Check if another thread is already loading
        if model_id in self.pending_loads:
            event = self.pending_loads[model_id]
            if event.wait(timeout):
                # Model should be loaded now
                return self.cache.get(model_id)
            else:
                logger.warning(f"Timeout waiting for {model_id} to load")
                return None
                
        # Start lazy loading
        self.stats['lazy_loads'] += 1
        return self._lazy_load_model(model_id)
        
    def _lazy_load_model(self, model_id: str) -> Optional[Any]:
        """
        Perform lazy loading of a model
        """
        # Create loading event
        load_event = threading.Event()
        self.pending_loads[model_id] = load_event
        
        try:
            loader_info = self.model_loaders[model_id]
            
            # Check memory before loading
            if not self._check_memory_available():
                self._trigger_aggressive_cleanup()
                
            # Load the model
            if loader_info['loader']:
                model = loader_info['loader']()
            else:
                model = self.load_model(
                    model_id, 
                    loader_info['path'],
                    loader_info['type']
                )
                
            # Signal completion
            load_event.set()
            
            return model
            
        except Exception as e:
            logger.error(f"Failed to lazy load {model_id}: {e}")
            load_event.set()
            return None
            
        finally:
            # Clean up pending load
            self.pending_loads.pop(model_id, None)
            
    def _track_access(self, model_id: str):
        """Track model access for predictive loading"""
        now = datetime.now()
        
        if model_id not in self.access_history:
            self.access_history[model_id] = []
            
        # Keep only recent history (last hour)
        cutoff = now - timedelta(hours=1)
        self.access_history[model_id] = [
            t for t in self.access_history[model_id] if t > cutoff
        ]
        
        # Add current access
        self.access_history[model_id].append(now)
        
        # Update predictions
        self._update_access_predictions()
        
    def _update_access_predictions(self):
        """Update model access predictions based on patterns"""
        now = datetime.now()
        
        for model_id, access_times in self.access_history.items():
            if not access_times:
                self.access_predictions[model_id] = 0.0
                continue
                
            # Calculate access frequency and recency
            access_count = len(access_times)
            if access_count > 0:
                # Time since last access
                time_since_last = (now - access_times[-1]).total_seconds()
                
                # Access rate (accesses per minute in last hour)
                access_rate = access_count / 60.0
                
                # Recency factor (exponential decay)
                recency_factor = np.exp(-time_since_last / 300)  # 5 min half-life
                
                # Combined prediction score
                prediction = min(1.0, access_rate * recency_factor)
                
                self.access_predictions[model_id] = prediction
                
                # Add to preload queue if high probability
                if prediction > self.config['preload_threshold']:
                    self.preload_queue[model_id] = prediction
                    
    def _check_memory_available(self) -> bool:
        """Check if enough memory is available for loading"""
        stats = self.get_memory_stats()
        
        # Conservative check - leave 20% buffer
        available_mb = stats.available_ram / 1024 / 1024
        return available_mb > self.config['max_memory_mb'] * 0.2
        
    def _trigger_aggressive_cleanup(self):
        """Aggressive memory cleanup for critical situations"""
        logger.warning("Triggering aggressive memory cleanup")
        
        # Use shorter timeout for aggressive cleanup
        timeout = self.config['aggressive_unload_timeout']
        
        current_time = datetime.now()
        models_to_unload = []
        
        with self.lock:
            for model_id, (model, metadata) in self.models.items():
                time_since_access = (current_time - metadata.last_access).total_seconds()
                
                # Unload if not accessed recently OR low prediction score
                prediction = self.access_predictions.get(model_id, 0)
                if time_since_access > timeout or prediction < 0.3:
                    models_to_unload.append(model_id)
                    
        # Unload models
        for model_id in models_to_unload:
            self.unload_model(model_id)
            
        # Clear cache if still needed
        if self.cache.current_memory > self.config['max_cache_memory_mb'] * 0.5 * 1024 * 1024:
            # Clear least recently used half of cache
            items_to_remove = len(self.cache.cache) // 2
            for _ in range(items_to_remove):
                if self.cache.cache:
                    self.cache.cache.popitem(last=False)
                    
        # Force garbage collection
        gc.collect()
        
    def preload_model_async(self, model_id: str):
        """
        Asynchronously preload a model based on prediction
        """
        if model_id in self.model_loaders and model_id not in self.models:
            # Check if worth preloading
            prediction = self.access_predictions.get(model_id, 0)
            if prediction > self.config['preload_threshold']:
                self.stats['preemptive_loads'] += 1
                
                # Load in background thread
                thread = threading.Thread(
                    target=self._lazy_load_model,
                    args=(model_id,),
                    daemon=True
                )
                thread.start()
                
    def _start_preloading(self):
        """Start predictive preloading thread"""
        self.preloading = True
        self.preload_thread = threading.Thread(
            target=self._preload_loop,
            daemon=True
        )
        self.preload_thread.start()
        
    def _preload_loop(self):
        """Background thread for predictive model loading"""
        while self.preloading:
            try:
                # Check preload queue
                if self.preload_queue:
                    # Sort by priority
                    sorted_queue = sorted(
                        self.preload_queue.items(),
                        key=lambda x: x[1],
                        reverse=True
                    )
                    
                    # Preload top N models
                    for model_id, priority in sorted_queue[:self.config['max_preload_queue']]:
                        if model_id not in self.models:
                            logger.info(f"Preloading {model_id} (priority: {priority:.2f})")
                            self.preload_model_async(model_id)
                            
                    # Clear queue
                    self.preload_queue.clear()
                    
            except Exception as e:
                logger.error(f"Preload error: {e}")
                
            time.sleep(10)  # Check every 10 seconds
            
    def _load_sklearn_model(self, model_path: Path) -> Any:
        """Load scikit-learn model with memory mapping if possible"""
        if self.config['enable_mmap']:
            try:
                # Use memory mapping for large files
                return joblib.load(model_path, mmap_mode='r')
            except Exception:
                return joblib.load(model_path)
        else:
            return joblib.load(model_path)
            
    def _load_onnx_model(self, model_path: Path) -> Any:
        """Load ONNX model with optimization"""
        # Configure session options for memory efficiency
        sess_options = ort.SessionOptions()
        sess_options.enable_cpu_mem_arena = False
        sess_options.enable_mem_pattern = False
        
        # Use minimal providers
        providers = ['CPUExecutionProvider']
        
        return ort.InferenceSession(
            str(model_path), 
            sess_options=sess_options,
            providers=providers
        )
        
    def _load_tensorflow_model(self, model_path: Path) -> Any:
        """Load TensorFlow model with optimization"""
        # Configure memory growth
        if tf.config.list_physical_devices('GPU'):
            for gpu in tf.config.list_physical_devices('GPU'):
                tf.config.experimental.set_memory_growth(gpu, True)
                
        # Load with optimization
        model = tf.keras.models.load_model(model_path)
        
        # Compile with optimization
        model.compile(
            optimizer='adam',
            loss='categorical_crossentropy',
            jit_compile=True  # XLA compilation
        )
        
        return model
        
    def _get_model_size(self, model: Any) -> int:
        """Estimate model size in bytes"""
        try:
            # For sklearn models
            if hasattr(model, '__sizeof__'):
                return model.__sizeof__()

            # For numpy arrays
            if hasattr(model, 'nbytes'):
                return model.nbytes

            # Estimate using pickle
            import pickle
            return len(pickle.dumps(model))
        except Exception:
            # Default estimate
            return 10 * 1024 * 1024  # 10MB
            
    def _quantize_model(self, model: Any, model_type: str) -> Any:
        """Quantize model to reduce memory usage - ultra mode for 30% target"""
        if model_type == 'sklearn':
            # Ultra-aggressive quantization for 30% target
            if self.config.get('enable_ultra_optimization'):
                # Use INT8 or even lower precision
                if hasattr(model, 'support_vectors_'):
                    # Quantize to INT8 with scaling
                    sv = model.support_vectors_
                    scale = np.max(np.abs(sv)) / 127
                    model._sv_scale = scale
                    model.support_vectors_ = (sv / scale).astype(np.int8)
                    
                if hasattr(model, 'dual_coef_'):
                    dc = model.dual_coef_
                    scale = np.max(np.abs(dc)) / 127
                    model._dc_scale = scale
                    model.dual_coef_ = (dc / scale).astype(np.int8)
                    
                if hasattr(model, 'coef_'):
                    # For linear models
                    coef = model.coef_
                    scale = np.max(np.abs(coef)) / 127
                    model._coef_scale = scale
                    model.coef_ = (coef / scale).astype(np.int8)
            else:
                # Standard float16 quantization
                if hasattr(model, 'support_vectors_'):
                    model.support_vectors_ = model.support_vectors_.astype(np.float16)
                if hasattr(model, 'dual_coef_'):
                    model.dual_coef_ = model.dual_coef_.astype(np.float16)
                
        elif model_type == 'tensorflow' and TF_OPTIMIZATION_AVAILABLE:
            # TensorFlow quantization
            converter = tf.lite.TFLiteConverter.from_keras_model(model)
            converter.optimizations = [tf.lite.Optimize.DEFAULT]
            converter.target_spec.supported_types = [tf.float16]
            tflite_model = converter.convert()
            
            # Create interpreter
            interpreter = tf.lite.Interpreter(model_content=tflite_model)
            interpreter.allocate_tensors()
            return interpreter
            
        return model
        
    def _compress_model(self, model: Any, model_type: str) -> Any:
        """Compress model to reduce memory usage"""
        if model_type == 'tensorflow' and TF_OPTIMIZATION_AVAILABLE:
            # Prune model
            pruning_params = {
                'pruning_schedule': tfmot.sparsity.keras.PolynomialDecay(
                    initial_sparsity=0.0,
                    final_sparsity=0.5,
                    begin_step=0,
                    end_step=100
                )
            }
            
            model = tfmot.sparsity.keras.prune_low_magnitude(model, **pruning_params)
            
        return model
        
    def unload_model(self, model_id: str):
        """Unload model from memory"""
        with self.lock:
            if model_id in self.models:
                model, metadata = self.models.pop(model_id)
                self.stats['models_unloaded'] += 1
                
                # Clear references
                del model
                gc.collect()
                
                logger.info(f"Model {model_id} unloaded")
                
    def unload_unused_models(self, timeout_seconds: int = 300):
        """Unload models that haven't been used recently"""
        current_time = datetime.now()
        models_to_unload = []
        
        with self.lock:
            for model_id, (model, metadata) in self.models.items():
                time_since_access = (current_time - metadata.last_access).total_seconds()
                if time_since_access > timeout_seconds:
                    models_to_unload.append(model_id)
                    
        for model_id in models_to_unload:
            self.unload_model(model_id)
            
    def _emergency_unload_all(self):
        """Emergency unload of all models (called by resource manager)"""
        logger.warning("Emergency unload of all ML models requested!")
        
        with self.lock:
            model_ids = list(self.models.keys())
            
        # Unload all models
        for model_id in model_ids:
            try:
                self.unload_model(model_id)
            except Exception as e:
                logger.error(f"Failed to unload {model_id}: {e}")
                
        # Clear cache
        self.cache.clear()
        
        # Force garbage collection
        gc.collect()
        
        logger.info("Emergency unload complete")
        
    def _handle_throttle_change(self, throttle_level: int):
        """Handle throttle level changes from resource manager"""
        logger.info(f"Throttle level changed to {throttle_level}")
        
        # Adjust behavior based on throttle level
        if throttle_level >= 3:  # Severe throttling
            # Disable predictive loading
            self.config['enable_predictive_loading'] = False
            # Reduce cache size
            self.cache.max_memory = self.config['max_cache_memory_mb'] * 0.5 * 1024 * 1024
        elif throttle_level >= 2:  # Moderate throttling
            # Reduce preload threshold
            self.config['preload_threshold'] = 0.9
        else:  # Low or no throttling
            # Restore normal settings
            self.config['enable_predictive_loading'] = True
            self.cache.max_memory = self.config['max_cache_memory_mb'] * 1024 * 1024
            
    def prepare_for_voice_unlock(self) -> bool:
        """
        Special preparation for voice unlock scenario with 30% memory target.
        Pre-compresses and optimizes voice model.
        """
        logger.info("Preparing ML system for voice unlock (30% memory target)")
        
        # First, clear everything else
        self._emergency_unload_all()
        
        # Pre-compress voice model if available
        voice_model_path = Path.home() / '.jarvis' / 'models' / 'voice_auth.pkl'
        if voice_model_path.exists():
            try:
                # Load and ultra-compress
                with open(voice_model_path, 'rb') as f:
                    model_data = f.read()
                
                # Compress with maximum compression
                import zlib
                compressed = zlib.compress(model_data, level=9)
                
                # Store in memory for quick access
                self.voice_model_compressed = compressed
                compress_ratio = len(compressed) / len(model_data)
                logger.info(f"Voice model pre-compressed to {compress_ratio:.1%} of original")
                
                return True
                
            except Exception as e:
                logger.error(f"Failed to prepare voice model: {e}")
                return False
                
        return False
        
    def load_voice_model_fast(self) -> Optional[Any]:
        """Ultra-fast loading of voice model for 30% memory target"""
        if self.current_ml_model == 'voice_auth':
            return self.models.get('voice_auth')
            
        # Resource manager check
        if RESOURCE_MANAGER_AVAILABLE:
            rm = get_resource_manager()
            if not rm.request_voice_unlock_resources():
                raise MemoryError("Cannot allocate resources for voice unlock")
                
        # Unload any other model
        if self.current_ml_model:
            self.unload_model(self.current_ml_model)
            
        # Ultra-fast load from compressed data
        if hasattr(self, 'voice_model_compressed'):
            try:
                import zlib
                model_data = zlib.decompress(self.voice_model_compressed)
                model = pickle.loads(model_data)
                
                # Apply ultra quantization
                model = self._quantize_model(model, 'sklearn')
                
                # Store
                self.models['voice_auth'] = model
                self.current_ml_model = 'voice_auth'
                
                logger.info("Voice model loaded from compressed cache")
                return model
                
            except Exception as e:
                logger.error(f"Fast load failed: {e}")
                
        # Fallback to normal load with ultra optimization
        return self.load_model('voice_auth', 
                             Path.home() / '.jarvis' / 'models' / 'voice_auth.pkl',
                             'sklearn')
    
    def get_memory_stats(self) -> MemoryStats:
        """Get current memory statistics"""
        process = psutil.Process()
        memory_info = process.memory_info()
        
        # System memory
        virtual_memory = psutil.virtual_memory()
        
        # ML memory estimation
        ml_memory = sum(
            metadata.size_bytes 
            for _, (_, metadata) in self.models.items()
        )
        
        return MemoryStats(
            total_ram=virtual_memory.total,
            available_ram=virtual_memory.available,
            used_ram=virtual_memory.used,
            percent_used=virtual_memory.percent,
            ml_memory_used=ml_memory,
            model_count=len(self.models),
            cache_hits=self.stats.get('cache_hits', 0),
            cache_misses=self.stats.get('cache_misses', 0)
        )
        
    def _start_monitoring(self):
        """Start memory monitoring thread"""
        self.monitoring = True
        self.monitor_thread = threading.Thread(target=self._monitor_memory, daemon=True)
        self.monitor_thread.start()
        
    def _monitor_memory(self):
        """Monitor memory usage and auto-unload if needed"""
        while self.monitoring:
            try:
                stats = self.get_memory_stats()
                
                # Ultra-aggressive memory management for 30% target
                if stats.percent_used > 25:  # Much lower threshold
                    logger.warning(f"Memory usage critical for 30% target: {stats.percent_used:.1f}%")
                    # Use panic timeout
                    self.unload_unused_models(self.config.get('panic_unload_timeout', 10))
                elif stats.percent_used > 20:
                    # Preemptive cleanup
                    self.unload_unused_models(self.config['aggressive_unload_timeout'])
                    
                # Log stats
                if len(self.models) > 0:
                    logger.debug(
                        f"ML Memory: {stats.ml_memory_used/1024/1024:.1f}MB, "
                        f"Models: {stats.model_count}, "
                        f"Cache hit rate: {self._get_cache_hit_rate():.1f}%"
                    )
                    
            except Exception as e:
                logger.error(f"Memory monitoring error: {e}")
                
            time.sleep(self.config['monitoring_interval'])
            
    def _get_cache_hit_rate(self) -> float:
        """Calculate cache hit rate"""
        hits = self.stats.get('cache_hits', 0)
        misses = self.stats.get('cache_misses', 0)
        total = hits + misses
        return (hits / total * 100) if total > 0 else 0.0
        
    def get_performance_report(self) -> Dict[str, Any]:
        """Get performance report"""
        stats = self.get_memory_stats()
        
        return {
            'memory': {
                'total_ram_mb': stats.total_ram / 1024 / 1024,
                'available_ram_mb': stats.available_ram / 1024 / 1024,
                'ml_memory_mb': stats.ml_memory_used / 1024 / 1024,
                'percent_used': stats.percent_used
            },
            'models': {
                'loaded': len(self.models),
                'total_loaded': self.stats['models_loaded'],
                'total_unloaded': self.stats['models_unloaded'],
                'avg_load_time': (
                    self.stats['total_load_time'] / self.stats['models_loaded']
                    if self.stats['models_loaded'] > 0 else 0
                )
            },
            'cache': {
                'size_mb': self.cache.current_memory / 1024 / 1024,
                'items': len(self.cache.cache),
                'hit_rate': self._get_cache_hit_rate()
            },
            'optimizations': {
                'quantization_enabled': self.config['enable_quantization'],
                'compression_enabled': self.config['enable_compression'],
                'mmap_enabled': self.config['enable_mmap']
            }
        }
        
    def cleanup(self):
        """Cleanup resources"""
        self.monitoring = False
        self.preloading = False
        
        if self.monitor_thread:
            self.monitor_thread.join(timeout=1)
            
        if self.preload_thread:
            self.preload_thread.join(timeout=1)
            
        # Unload all models
        for model_id in list(self.models.keys()):
            self.unload_model(model_id)
            
        # Clear cache
        self.cache.clear()
        
        # Clear loaders
        self.model_loaders.clear()
        self.pending_loads.clear()
        
        logger.info("ML Model Manager cleaned up")


# Singleton instance
_ml_manager = None


def get_ml_manager(config: Optional[Dict[str, Any]] = None) -> MLModelManager:
    """Get singleton ML manager instance"""
    global _ml_manager
    if _ml_manager is None:
        _ml_manager = MLModelManager(config)
    return _ml_manager