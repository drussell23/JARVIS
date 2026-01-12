#!/usr/bin/env python3
"""
Lazy Vision Engine - Deferred Model Loading for Fast Startup
All ML models are loaded on-demand or through parallel loading system
"""

import asyncio
import logging
from typing import Dict, List, Optional, Tuple, Any, Callable
from dataclasses import dataclass, field
from datetime import datetime
import json
import numpy as np
from pathlib import Path
import pickle
from collections import defaultdict
import re
import inspect
import threading

logger = logging.getLogger(__name__)

class LazyModelLoader:
    """Lazy loading wrapper for ML models"""
    
    def __init__(self, model_loader_func: Callable, model_name: str):
        self._model = None
        self._loader_func = model_loader_func
        self._model_name = model_name
        self._loading = False
        self._lock = threading.Lock()
        
    def get(self):
        """Get model, loading if necessary"""
        if self._model is None and not self._loading:
            with self._lock:
                if self._model is None:  # Double-check pattern
                    logger.info(f"Lazy loading model: {self._model_name}")
                    self._loading = True
                    try:
                        self._model = self._loader_func()
                        logger.info(f"Model loaded: {self._model_name}")
                    except Exception as e:
                        logger.error(f"Failed to load {self._model_name}: {e}")
                        self._model = None
                    finally:
                        self._loading = False
        return self._model
        
    def is_loaded(self) -> bool:
        """Check if model is loaded"""
        return self._model is not None
        
    def preload(self):
        """Explicitly preload the model"""
        return self.get()

@dataclass
class VisionCapability:
    """Dynamically discovered vision capability"""
    name: str
    description: str
    handler: Callable
    confidence_threshold: float = 0.7
    learned_patterns: List[str] = field(default_factory=list)
    success_rate: float = 0.0
    usage_count: int = 0
    last_used: Optional[datetime] = None
    parameters: Dict[str, Any] = field(default_factory=dict)
    examples: List[str] = field(default_factory=list)

@dataclass
class VisionIntent:
    """ML-analyzed vision intent"""
    raw_command: str
    action_verb: Optional[str] = None
    target_object: Optional[str] = None
    modifiers: List[str] = field(default_factory=list)
    confidence_scores: Dict[str, float] = field(default_factory=dict)
    context_clues: Dict[str, Any] = field(default_factory=dict)
    semantic_embedding: Optional[np.ndarray] = None
    predicted_capability: Optional[str] = None

class LazyDynamicVisionEngine:
    """
    Vision engine with lazy model loading for fast startup
    Models are loaded only when first used or via parallel loading
    """
    
    def __init__(self):
        # Dynamic capability registry
        self.capabilities: Dict[str, VisionCapability] = {}
        
        # Lazy ML components
        self._semantic_model = None
        self._intent_classifier = None
        
        # Learning system
        self.command_history = []
        self.feedback_data = []
        self.user_preferences = defaultdict(float)
        
        # Dynamic pattern learning
        self.learned_patterns = defaultdict(list)
        self.pattern_scores = defaultdict(float)
        self.pattern_embeddings = {}
        
        # Context awareness
        self.context_memory = []
        self.session_context = {}
        
        # Plugin system for vision providers
        self.vision_providers = {}
        
        # Model loading status
        self._models_loaded = False
        
        logger.info("Lazy Dynamic Vision Engine initialized - models will load on demand")
        
    def _load_semantic_model(self):
        """Load semantic understanding model"""
        try:
            from sentence_transformers import SentenceTransformer
            return SentenceTransformer('all-MiniLM-L6-v2')
        except ImportError:
            logger.info("Sentence transformers not available, using fallback")
            return None
            
    def _load_intent_classifier(self):
        """Load intent classification system"""
        # Import and initialize on demand
        try:
            from .ml_intent_classifier import MLIntentClassifier
            return MLIntentClassifier()
        except ImportError:
            logger.warning("ML intent classifier not available")
            return None
            
    @property
    def semantic_model(self):
        """Lazy-load semantic model"""
        if self._semantic_model is None:
            self._semantic_model = LazyModelLoader(
                self._load_semantic_model,
                "semantic_model"
            )
        return self._semantic_model.get()
        
    @property
    def intent_classifier(self):
        """Lazy-load intent classifier"""
        if self._intent_classifier is None:
            self._intent_classifier = LazyModelLoader(
                self._load_intent_classifier,
                "intent_classifier"
            )
        return self._intent_classifier.get()
        
    async def initialize_models_parallel(self, executor=None):
        """Initialize all models in parallel - called by ML loader"""
        logger.info("Initializing vision models in parallel")
        
        try:
            # List of models to load
            models_to_load = []
            
            if self._semantic_model is None:
                self._semantic_model = LazyModelLoader(
                    self._load_semantic_model,
                    "semantic_model"
                )
                models_to_load.append(self._semantic_model)
                
            if self._intent_classifier is None:
                self._intent_classifier = LazyModelLoader(
                    self._load_intent_classifier,
                    "intent_classifier"
                )
                models_to_load.append(self._intent_classifier)
                
            # Load models in parallel
            if executor and models_to_load:
                loop = asyncio.get_event_loop()
                tasks = []
                for model in models_to_load:
                    task = loop.run_in_executor(executor, model.preload)
                    tasks.append(task)
                
                # Wait with timeout to prevent hanging
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*tasks, return_exceptions=True),
                        timeout=30.0  # 30 second timeout
                    )
                except asyncio.TimeoutError:
                    logger.warning("Model loading timed out after 30 seconds")
                
            # Discover providers after models are loaded
            await self._discover_vision_providers_async()
            
            # Load saved data
            await self._load_learned_data_async()
            
            self._models_loaded = True
            logger.info("Vision models initialized successfully")
            
        except Exception as e:
            logger.error(f"Error during vision model initialization: {e}")
            self._models_loaded = True  # Mark as loaded to prevent hanging
        
    async def _discover_vision_providers_async(self):
        """Discover vision providers asynchronously"""
        try:
            # Use to_thread if available (Python 3.9+)
            if hasattr(asyncio, 'to_thread'):
                await asyncio.to_thread(self._discover_vision_providers)
            else:
                # Fallback for older Python
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._discover_vision_providers)
        except Exception as e:
            logger.error(f"Error discovering vision providers: {e}")
        
    async def _load_learned_data_async(self):
        """Load learned data asynchronously"""
        try:
            # Use to_thread if available (Python 3.9+)
            if hasattr(asyncio, 'to_thread'):
                await asyncio.to_thread(self._load_learned_data)
            else:
                # Fallback for older Python
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._load_learned_data)
        except Exception as e:
            logger.error(f"Error loading learned data: {e}")
        
    def _discover_vision_providers(self):
        """Dynamically discover available vision providers"""
        # Discover basic capabilities first (no ML needed)
        self._register_basic_capabilities()
        
        # Discover from available modules
        vision_modules = [
            'intelligent_vision_integration',
            'screen_vision',
            'claude_vision_analyzer',
            'jarvis_workspace_integration',
            'enhanced_monitoring',
            'screen_capture_fallback'
        ]
        
        for module_name in vision_modules:
            try:
                module = __import__(module_name, fromlist=[''])
                self._scan_module_for_capabilities(module, module_name)
                logger.info(f"Discovered vision module: {module_name}")
            except ImportError:
                continue
                
    def _register_basic_capabilities(self):
        """Register basic capabilities that don't need ML"""
        # Basic screen capture
        async def basic_capture_screen():
            """Basic screen capture without ML"""
            try:
                from .screen_capture_fallback import capture_screen_simple
                return await capture_screen_simple()
            except Exception:
                return {"error": "Screen capture not available"}
                
        self.register_capability(
            "basic_capture_screen",
            "Capture the current screen",
            basic_capture_screen
        )
        
        logger.info("Registered basic vision capabilities")
        
    def _scan_module_for_capabilities(self, module, module_name: str):
        """Scan a module for vision capabilities"""
        for name, obj in inspect.getmembers(module):
            if inspect.isclass(obj):
                # Look for classes with vision-related methods
                for method_name, method in inspect.getmembers(obj):
                    if (method_name.startswith(('analyze', 'detect', 'capture', 'monitor')) 
                        and callable(method)):
                        capability_name = f"{module_name}.{obj.__name__}.{method_name}"
                        self.register_capability(
                            capability_name,
                            getattr(method, '__doc__', f"{method_name} from {obj.__name__}"),
                            method
                        )
                        
    def register_capability(self, name: str, description: str, handler: Callable,
                          confidence_threshold: float = 0.7, examples: List[str] = None):
        """Register a vision capability dynamically"""
        capability = VisionCapability(
            name=name,
            description=description,
            handler=handler,
            confidence_threshold=confidence_threshold,
            examples=examples or []
        )
        
        self.capabilities[name] = capability
        
        # Only compute embeddings if model is loaded
        if self._semantic_model and self._semantic_model.is_loaded():
            self._update_capability_embeddings(name, description)
            
        logger.info(f"Registered vision capability: {name}")
        
    def _update_capability_embeddings(self, name: str, description: str):
        """Update embeddings for capability matching"""
        if self.semantic_model:
            embedding = self.semantic_model.encode(description)
            self.pattern_embeddings[name] = embedding
            
    def _load_learned_data(self):
        """Load previously learned patterns and preferences"""
        data_file = Path("backend/data/vision_learned_patterns.pkl")
        if data_file.exists():
            try:
                with open(data_file, 'rb') as f:
                    data = pickle.load(f)
                    self.learned_patterns = data.get('patterns', defaultdict(list))
                    self.pattern_scores = data.get('scores', defaultdict(float))
                    self.user_preferences = data.get('preferences', defaultdict(float))
                logger.info("Loaded learned vision patterns")
            except Exception as e:
                logger.error(f"Failed to load learned data: {e}")
                
    async def save_learned_data(self):
        """Save learned patterns and preferences"""
        data_file = Path("backend/data/vision_learned_patterns.pkl")
        data_file.parent.mkdir(parents=True, exist_ok=True)
        
        data = {
            'patterns': dict(self.learned_patterns),
            'scores': dict(self.pattern_scores),
            'preferences': dict(self.user_preferences)
        }
        
        with open(data_file, 'wb') as f:
            pickle.dump(data, f)
            
    async def analyze_command(self, command: str) -> VisionIntent:
        """Analyze vision command using ML to extract intent"""
        # If models not loaded yet, load them
        if not self._models_loaded:
            logger.info("Models not loaded, using basic analysis")
            return VisionIntent(
                raw_command=command,
                action_verb="analyze",
                target_object="screen"
            )
            
        intent = VisionIntent(raw_command=command)
        
        # Use NLP to extract components if available
        if self.intent_classifier:
            classification = await self.intent_classifier.classify(command)
            intent.action_verb = classification.get('action')
            intent.target_object = classification.get('object')
            intent.modifiers = classification.get('modifiers', [])
            intent.confidence_scores = classification.get('confidence', {})
            
        # Generate semantic embedding if model available
        if self.semantic_model:
            intent.semantic_embedding = self.semantic_model.encode(command)
            
        # Predict best capability
        intent.predicted_capability = await self._predict_capability(intent)
        
        return intent
        
    async def _predict_capability(self, intent: VisionIntent) -> Optional[str]:
        """Predict best capability for intent using ML"""
        if not intent.semantic_embedding or not self.pattern_embeddings:
            # Fallback to keyword matching if no embeddings
            return self._basic_capability_match(intent)
            
        # Find most similar capability using cosine similarity
        best_score = -1
        best_capability = None
        
        for cap_name, cap_embedding in self.pattern_embeddings.items():
            similarity = np.dot(intent.semantic_embedding, cap_embedding) / (
                np.linalg.norm(intent.semantic_embedding) * np.linalg.norm(cap_embedding)
            )
            
            # Apply learned preferences
            if cap_name in self.user_preferences:
                similarity *= (1 + self.user_preferences[cap_name])
                
            if similarity > best_score:
                best_score = similarity
                best_capability = cap_name
                
        return best_capability if best_score > 0.5 else None
        
    def _basic_capability_match(self, intent: VisionIntent) -> Optional[str]:
        """Basic capability matching without ML"""
        command_lower = intent.raw_command.lower()
        
        # Simple keyword matching as fallback
        for name, capability in self.capabilities.items():
            if any(keyword in command_lower for keyword in name.split('_')):
                return name
                
        return "basic_capture_screen"  # Default fallback
        
    async def process_vision_command(self, command: str) -> Dict[str, Any]:
        """Process vision command - alias for execute_vision_command for compatibility"""
        return await self.execute_vision_command(command)
    
    async def execute_vision_command(self, command: str) -> Dict[str, Any]:
        """Execute vision command with zero hardcoding"""
        # Analyze intent
        intent = await self.analyze_command(command)
        
        # Get predicted capability
        capability_name = intent.predicted_capability
        
        if not capability_name or capability_name not in self.capabilities:
            # Try to find best match
            capability_name = await self._find_best_capability(intent)
            
        if not capability_name:
            return {
                "error": "No suitable vision capability found",
                "suggestion": "Try being more specific about what you want to see",
                "available_capabilities": list(self.capabilities.keys())
            }
            
        # Execute capability
        capability = self.capabilities[capability_name]
        
        try:
            # Track usage
            capability.usage_count += 1
            capability.last_used = datetime.now()
            
            # Execute handler
            if asyncio.iscoroutinefunction(capability.handler):
                result = await capability.handler()
            else:
                result = capability.handler()
                
            # Track success
            self._record_usage(command, capability_name, success=True)
            
            return {
                "success": True,
                "capability_used": capability_name,
                "confidence": intent.confidence_scores.get('overall', 0.8),
                "result": result
            }
            
        except Exception as e:
            logger.error(f"Failed to execute capability {capability_name}: {e}")
            self._record_usage(command, capability_name, success=False)
            
            return {
                "error": f"Failed to execute vision command: {str(e)}",
                "capability_attempted": capability_name
            }
            
    async def _find_best_capability(self, intent: VisionIntent) -> Optional[str]:
        """Find best capability using multiple strategies"""
        # Strategy 1: Semantic similarity (if available)
        if self.semantic_model and intent.semantic_embedding is not None:
            return await self._predict_capability(intent)
            
        # Strategy 2: Basic matching
        return self._basic_capability_match(intent)
        
    def _record_usage(self, command: str, capability: str, success: bool):
        """Record usage for learning"""
        self.command_history.append({
            'command': command,
            'capability': capability,
            'success': success,
            'timestamp': datetime.now()
        })
        
        # Update preferences
        if success:
            self.user_preferences[capability] = min(1.0, self.user_preferences[capability] + 0.1)
            self.pattern_scores[f"{command}|{capability}"] += 1
        else:
            self.user_preferences[capability] = max(-1.0, self.user_preferences[capability] - 0.05)
            
        # Save periodically
        if len(self.command_history) % 10 == 0:
            asyncio.create_task(self.save_learned_data())
            
    def get_model_info(self) -> Dict[str, Any]:
        """Get information about loaded models"""
        return {
            'semantic_model': {
                'loaded': self._semantic_model.is_loaded() if self._semantic_model else False,
                'type': 'SentenceTransformer'
            },
            'intent_classifier': {
                'loaded': self._intent_classifier.is_loaded() if self._intent_classifier else False,
                'type': 'MLIntentClassifier'
            },
            'capabilities_count': len(self.capabilities),
            'learned_patterns': len(self.learned_patterns),
            'models_initialized': self._models_loaded
        }

# Global instance for easy access
_lazy_vision_engine = None

def get_lazy_vision_engine() -> LazyDynamicVisionEngine:
    """Get or create the global lazy vision engine"""
    global _lazy_vision_engine
    if _lazy_vision_engine is None:
        _lazy_vision_engine = LazyDynamicVisionEngine()
    return _lazy_vision_engine

async def initialize_vision_engine_models(executor=None):
    """Initialize vision engine models - called by parallel loader"""
    engine = get_lazy_vision_engine()
    await engine.initialize_models_parallel(executor)
    return engine