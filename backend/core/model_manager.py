"""Model Manager - Brain Switcher for JARVIS.

This module implements a tiered model loading system with automatic switching based on
task complexity and available memory. It manages multiple language models of different
sizes and capabilities, ensuring optimal performance while respecting memory constraints.

The system uses three tiers:
- TINY: Always-loaded lightweight model for instant responses
- STANDARD: Medium-capability model loaded on demand
- ADVANCED: High-capability model for complex tasks

Example:
    >>> manager = ModelManager()
    >>> model, tier = await manager.get_model_for_task("code", 0.8, 1000)
    >>> print(f"Selected {tier.value} model")
"""

import os
import logging
import psutil
import asyncio
from pathlib import Path
from typing import Dict, Optional, Any, Tuple, List
from enum import Enum
from datetime import datetime, timedelta
import json

logger = logging.getLogger(__name__)


class ModelTier(Enum):
    """Model tiers for different complexity levels.
    
    Attributes:
        TINY: Lightweight model (1GB) - Always loaded for instant responses
        STANDARD: Medium model (2GB) - Loaded on demand for balanced tasks
        ADVANCED: Heavy model (4GB) - For complex tasks requiring high capability
    """
    TINY = "tiny"      # TinyLlama - 1GB - Always loaded
    STANDARD = "std"   # Phi-2 - 2GB - On-demand
    ADVANCED = "adv"   # Mistral-7B - 4GB - Complex tasks


class ModelInfo:
    """Information about a language model.
    
    Stores metadata about a model including its capabilities, performance metrics,
    and usage statistics for intelligent model management.
    
    Attributes:
        name: Human-readable model name
        path: File path to the model
        size_gb: Model size in gigabytes
        tier: Model tier classification
        capabilities: Dictionary of capability scores (0-1) for different task types
        context_size: Maximum context length in tokens
        last_used: Timestamp of last usage
        load_count: Number of times model has been loaded
        avg_response_time: Average response time in seconds
    """
    
    def __init__(self, name: str, path: str, size_gb: float, tier: ModelTier, 
                 capabilities: Dict[str, float], context_size: int = 2048) -> None:
        """Initialize model information.
        
        Args:
            name: Human-readable name for the model
            path: Relative path to model file
            size_gb: Model size in gigabytes
            tier: Model tier classification
            capabilities: Dictionary mapping task types to capability scores (0-1)
            context_size: Maximum context length in tokens
        """
        self.name = name
        self.path = path
        self.size_gb = size_gb
        self.tier = tier
        self.capabilities = capabilities  # Scores for different task types
        self.context_size = context_size
        self.last_used: Optional[datetime] = None
        self.load_count = 0
        self.avg_response_time = 0.0


class ModelManager:
    """Manages tiered model loading and switching.
    
    This class implements intelligent model management with automatic tier selection
    based on task complexity, memory pressure, and performance requirements. It
    maintains a registry of available models and handles loading/unloading based
    on system resources.
    
    Attributes:
        models_dir: Directory containing model files
        models: Registry of available models by tier
        loaded_models: Cache of currently loaded models
        performance_stats: Performance tracking data
        memory_thresholds: Memory usage thresholds for different states
    """
    
    def __init__(self, models_dir: str = "models") -> None:
        """Initialize the model manager.
        
        Sets up model registry, creates models directory if needed, and schedules
        loading of the tiny model for instant responses.
        
        Args:
            models_dir: Directory path for storing model files
        """
        self.models_dir = Path(models_dir)
        self.models_dir.mkdir(exist_ok=True)
        
        # Model registry
        self.models: Dict[ModelTier, ModelInfo] = {
            ModelTier.TINY: ModelInfo(
                name="TinyLlama-1.1B",
                path="tinyllama-1.1b.gguf",
                size_gb=1.0,
                tier=ModelTier.TINY,
                capabilities={
                    "chat": 0.7,
                    "code": 0.5,
                    "analysis": 0.4,
                    "creative": 0.6
                },
                context_size=2048
            ),
            ModelTier.STANDARD: ModelInfo(
                name="Phi-2",
                path="phi-2.gguf",
                size_gb=2.0,
                tier=ModelTier.STANDARD,
                capabilities={
                    "chat": 0.85,
                    "code": 0.8,
                    "analysis": 0.75,
                    "creative": 0.8
                },
                context_size=2048
            ),
            ModelTier.ADVANCED: ModelInfo(
                name="Mistral-7B",
                path="mistral-7b-instruct.gguf",
                size_gb=4.0,
                tier=ModelTier.ADVANCED,
                capabilities={
                    "chat": 0.95,
                    "code": 0.9,
                    "analysis": 0.95,
                    "creative": 0.9
                },
                context_size=4096
            )
        }
        
        # Loaded models cache
        self.loaded_models: Dict[ModelTier, Any] = {}
        
        # Performance tracking
        self.performance_stats: Dict[str, Any] = {}
        
        # Memory thresholds
        self.memory_thresholds = {
            "critical": 85,  # Unload all but tiny
            "high": 75,      # Unload advanced
            "moderate": 60,  # Normal operation
            "low": 40        # Can load any model
        }
        
        # Initialize with tiny model
        asyncio.create_task(self._ensure_tiny_loaded())
        
    async def _ensure_tiny_loaded(self) -> None:
        """Ensure tiny model is always loaded for instant responses.
        
        This method is called during initialization to preload the lightweight
        model that provides immediate responses while larger models load.
        
        Raises:
            Exception: If tiny model fails to load (logged but not re-raised)
        """
        try:
            await self.load_model(ModelTier.TINY)
            logger.info("Tiny model loaded for instant responses")
        except Exception as e:
            logger.error(f"Failed to load tiny model: {e}")
            
    async def get_model_for_task(self, task_type: str, complexity: float, 
                                 context_length: int) -> Tuple[Any, ModelTier]:
        """Get the best model for a given task.
        
        Analyzes task requirements and system resources to select the optimal
        model tier, then loads the model if necessary.
        
        Args:
            task_type: Type of task (e.g., "chat", "code", "analysis", "creative")
            complexity: Task complexity score from 0.0 to 1.0
            context_length: Required context length in tokens
            
        Returns:
            Tuple containing the loaded model instance and selected tier
            
        Raises:
            FileNotFoundError: If required model file doesn't exist
            Exception: If model loading fails
            
        Example:
            >>> model, tier = await manager.get_model_for_task("code", 0.8, 1500)
            >>> response = model.invoke("Write a Python function")
        """
        # Check memory pressure
        memory_state = self._get_memory_state()
        
        # Determine required tier based on complexity
        required_tier = self._determine_required_tier(task_type, complexity, context_length)
        
        # Adjust based on memory
        selected_tier = self._adjust_tier_for_memory(required_tier, memory_state)
        
        # Load model if needed
        model = await self._get_or_load_model(selected_tier)
        
        return model, selected_tier
        
    def _determine_required_tier(self, task_type: str, complexity: float, 
                                context_length: int) -> ModelTier:
        """Determine the required model tier based on task characteristics.
        
        Analyzes task requirements to select the minimum tier that can handle
        the task effectively, considering context length and complexity.
        
        Args:
            task_type: Type of task being performed
            complexity: Task complexity score (0.0 to 1.0)
            context_length: Required context length in tokens
            
        Returns:
            The minimum model tier required for the task
        """
        # Context length check
        if context_length > 3000:
            return ModelTier.ADVANCED  # Only Mistral has 4k context
            
        # Complexity-based selection
        if complexity > 0.8:
            return ModelTier.ADVANCED
        elif complexity > 0.5:
            return ModelTier.STANDARD
        else:
            return ModelTier.TINY
            
    def _get_memory_state(self) -> str:
        """Get current system memory state.
        
        Analyzes current memory usage and returns a state classification
        used for model loading decisions.
        
        Returns:
            Memory state: "critical", "high", "moderate", or "low"
        """
        mem = psutil.virtual_memory()
        percent = mem.percent
        
        if percent >= self.memory_thresholds["critical"]:
            return "critical"
        elif percent >= self.memory_thresholds["high"]:
            return "high"
        elif percent >= self.memory_thresholds["moderate"]:
            return "moderate"
        else:
            return "low"
            
    def _adjust_tier_for_memory(self, requested_tier: ModelTier, 
                                memory_state: str) -> ModelTier:
        """Adjust tier selection based on memory pressure.
        
        Downgrades model tier selection when memory usage is high to prevent
        system instability and out-of-memory errors.
        
        Args:
            requested_tier: Originally requested model tier
            memory_state: Current memory state classification
            
        Returns:
            Adjusted model tier that respects memory constraints
        """
        if memory_state == "critical":
            # Only use tiny model
            return ModelTier.TINY
        elif memory_state == "high":
            # Downgrade from advanced if requested
            if requested_tier == ModelTier.ADVANCED:
                return ModelTier.STANDARD
        # Otherwise use requested tier
        return requested_tier
        
    async def _get_or_load_model(self, tier: ModelTier) -> Any:
        """Get model from cache or load if necessary.
        
        Retrieves a model from the loaded models cache, or loads it if not
        currently in memory. Updates usage statistics.
        
        Args:
            tier: Model tier to retrieve
            
        Returns:
            The loaded model instance
            
        Raises:
            FileNotFoundError: If model file doesn't exist
            Exception: If model loading fails
        """
        if tier in self.loaded_models:
            model = self.loaded_models[tier]
            self.models[tier].last_used = datetime.now()
            return model
            
        # Need to load model
        return await self.load_model(tier)
        
    async def load_model(self, tier: ModelTier) -> Any:
        """Load a model into memory.
        
        Loads the specified model tier into memory, managing system resources
        and unloading other models if necessary to free space.
        
        Args:
            tier: Model tier to load
            
        Returns:
            The loaded model instance
            
        Raises:
            FileNotFoundError: If model file doesn't exist
            Exception: If model loading fails due to insufficient memory or other issues
            
        Example:
            >>> model = await manager.load_model(ModelTier.STANDARD)
            >>> response = model.invoke("Hello, world!")
        """
        model_info = self.models[tier]
        model_path = self.models_dir / model_info.path
        
        if not model_path.exists():
            raise FileNotFoundError(f"Model not found: {model_path}")
            
        # Check if we need to unload other models first
        await self._manage_memory_for_load(model_info.size_gb)
        
        logger.info(f"Loading {tier.value} model: {model_info.name}")
        
        try:
            # Import here to avoid circular dependencies
            try:
                from langchain_community.llms import LlamaCpp
                
                # Create model with optimized settings
                model = LlamaCpp(
                    model_path=str(model_path),
                    n_gpu_layers=int(os.getenv("JARVIS_N_GPU_LAYERS", "-1")),  # v234.0: Full Metal GPU offload
                    n_ctx=model_info.context_size,
                    n_batch=256 if tier == ModelTier.TINY else 512,
                    temperature=0.7,
                    max_tokens=256,
                    n_threads=4 if tier == ModelTier.TINY else 6,
                    use_mlock=True,
                    verbose=False,
                    f16_kv=True,
                    streaming=False
                )
            except ImportError:
                logger.warning("LlamaCpp not available, using mock model for testing")
                # Create a mock model for testing
                class MockLLM:
                    """Mock LLM for testing when LlamaCpp is not available."""
                    
                    def __init__(self, **kwargs) -> None:
                        self.model_path = kwargs.get('model_path', '')
                        self.n_ctx = kwargs.get('n_ctx', 2048)
                    
                    def __call__(self, prompt: str, **kwargs) -> str:
                        return f"Mock response from {model_info.name} for: {prompt[:50]}..."
                    
                    def invoke(self, prompt: str, **kwargs) -> str:
                        return self.__call__(prompt, **kwargs)
                
                model = MockLLM(
                    model_path=str(model_path),
                    n_ctx=model_info.context_size
                )
            
            # Cache the model
            self.loaded_models[tier] = model
            model_info.load_count += 1
            model_info.last_used = datetime.now()
            
            logger.info(f"Successfully loaded {model_info.name}")
            return model
            
        except Exception as e:
            logger.error(f"Failed to load {model_info.name}: {e}")
            raise
            
    async def _manage_memory_for_load(self, required_gb: float) -> None:
        """Manage memory before loading a new model.
        
        Checks available memory and unloads models if necessary to ensure
        sufficient space for loading the requested model.
        
        Args:
            required_gb: Memory required for the new model in gigabytes
        """
        mem = psutil.virtual_memory()
        available_gb = mem.available / (1024**3)
        
        if available_gb < required_gb + 1:  # Keep 1GB buffer
            # Need to unload models
            await self._unload_least_used_models(required_gb + 1 - available_gb)
            
    async def _unload_least_used_models(self, needed_gb: float) -> None:
        """Unload least recently used models to free memory.
        
        Unloads models in order of least recent usage until sufficient
        memory is freed. Never unloads the tiny model.
        
        Args:
            needed_gb: Amount of memory to free in gigabytes
        """
        freed_gb = 0
        
        # Sort loaded models by last used time (exclude tiny)
        loaded_tiers = [
            (tier, info) for tier, info in self.models.items()
            if tier in self.loaded_models and tier != ModelTier.TINY
        ]
        loaded_tiers.sort(key=lambda x: x[1].last_used or datetime.min)
        
        for tier, info in loaded_tiers:
            if freed_gb >= needed_gb:
                break
                
            logger.info(f"Unloading {info.name} to free memory")
            await self.unload_model(tier)
            freed_gb += info.size_gb
            
    async def unload_model(self, tier: ModelTier) -> None:
        """Unload a model from memory.
        
        Removes a model from the loaded models cache and forces garbage
        collection to free memory immediately.
        
        Args:
            tier: Model tier to unload
        """
        if tier in self.loaded_models:
            # Explicitly delete to free memory
            del self.loaded_models[tier]
            
            # Force garbage collection
            import gc
            gc.collect()
            
            logger.info(f"Unloaded {self.models[tier].name}")
            
    def get_model_stats(self) -> Dict[str, Any]:
        """Get statistics about model usage and system state.
        
        Returns comprehensive statistics about loaded models, memory usage,
        and performance metrics for monitoring and debugging.
        
        Returns:
            Dictionary containing:
                - loaded_models: List of currently loaded models with metadata
                - memory_state: Current memory pressure state
                - total_models: Total number of available models
                - loaded_count: Number of currently loaded models
                
        Example:
            >>> stats = manager.get_model_stats()
            >>> print(f"Memory state: {stats['memory_state']}")
            >>> print(f"Loaded models: {len(stats['loaded_models'])}")
        """
        stats = {
            "loaded_models": [
                {
                    "tier": tier.value,
                    "name": self.models[tier].name,
                    "size_gb": self.models[tier].size_gb,
                    "last_used": self.models[tier].last_used.isoformat() 
                               if self.models[tier].last_used else None,
                    "load_count": self.models[tier].load_count
                }
                for tier in self.loaded_models
            ],
            "memory_state": self._get_memory_state(),
            "total_models": len(self.models),
            "loaded_count": len(self.loaded_models)
        }
        return stats
        
    async def optimize_for_workload(self, recent_tasks: List[Dict[str, Any]]) -> None:
        """Optimize model loading based on recent workload patterns.
        
        Analyzes recent task history to predict future needs and preload
        appropriate models to reduce response latency.
        
        Args:
            recent_tasks: List of recent task dictionaries containing task metadata
                Each task should have at least a "type" field indicating task type
                
        Example:
            >>> recent_tasks = [
            ...     {"type": "code", "complexity": 0.8},
            ...     {"type": "code", "complexity": 0.7},
            ...     {"type": "analysis", "complexity": 0.9}
            ... ]
            >>> await manager.optimize_for_workload(recent_tasks)
        """
        # Analyze recent tasks to predict future needs
        task_types: Dict[str, int] = {}
        for task in recent_tasks:
            task_type = task.get("type", "chat")
            task_types[task_type] = task_types.get(task_type, 0) + 1
            
        # Preload models based on patterns
        if task_types.get("code", 0) > 5:
            # Lots of coding tasks, ensure standard model is loaded
            await self._get_or_load_model(ModelTier.STANDARD)
        elif task_types.get("analysis", 0) > 3:
            # Complex analysis, consider loading advanced
            if self._get_memory_state() in ["low", "moderate"]:
                await self._get_or_load_model(ModelTier.ADVANCED)