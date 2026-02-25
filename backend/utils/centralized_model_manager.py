#!/usr/bin/env python3
"""
Centralized Model Manager for JARVIS
Prevents duplicate model loading and manages model lifecycle
"""

import os
import logging
from typing import Dict, Any, Optional
from functools import lru_cache
from threading import Lock

logger = logging.getLogger(__name__)

class CentralizedModelManager:
    """Singleton model manager to prevent duplicate model loading"""
    
    _instance = None
    _lock = Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
            
        self._initialized = True
        self._models = {}
        self._model_locks = {}
        logger.info("Centralized Model Manager initialized")
    
    @lru_cache(maxsize=1)
    def get_whisper_model(self, model_size: str = "base") -> Any:
        """Get or load Whisper model (cached)"""
        import whisper

        key = f"whisper_{model_size}"

        if key not in self._models:
            logger.info(f"Loading Whisper model: {model_size}")
            self._models[key] = whisper.load_model(model_size)
            logger.info(f"Whisper model {model_size} loaded successfully")

        return self._models[key]
    
    @lru_cache(maxsize=1)
    def get_whisper_tiny(self) -> Any:
        """Get Whisper tiny model for wake word detection"""
        return self.get_whisper_model("tiny")
    
    def get_claude_vision_analyzer(self) -> Optional[Any]:
        """Get Claude Vision Analyzer instance"""
        if "claude_vision" not in self._models:
            api_key = os.getenv("ANTHROPIC_API_KEY")
            if api_key:
                try:
                    from vision.claude_vision_analyzer import ClaudeVisionAnalyzer
                    self._models["claude_vision"] = ClaudeVisionAnalyzer(api_key)
                    logger.info("Claude Vision Analyzer initialized")
                except Exception as e:
                    logger.error(f"Failed to initialize Claude Vision: {e}")
                    return None
            else:
                logger.warning("No Anthropic API key found")
                return None
        
        return self._models.get("claude_vision")
    
    def load_custom_model(self, model_path: str, key: str) -> Optional[Any]:
        """Load a custom PyTorch model"""
        if key not in self._models:
            if os.path.exists(model_path):
                try:
                    import torch
                    self._models[key] = torch.load(model_path, map_location='cpu')
                    logger.info(f"Loaded custom model: {key}")
                except Exception as e:
                    logger.error(f"Failed to load custom model {key}: {e}")
                    return None
            else:
                logger.warning(f"Model file not found: {model_path}")
                return None

        return self._models.get(key)
    
    def get_spacy_model(self, model_name: str = "en_core_web_sm") -> Optional[Any]:
        """Get or load spaCy model (singleton)"""
        key = f"spacy_{model_name}"
        
        if key not in self._models:
            try:
                                self._models[key] =                 logger.info(f"Loaded spaCy model: {model_name}")
            except Exception as e:
                logger.error(f"Failed to load spaCy model {model_name}: {e}")
                return None
        
        return self._models.get(key)
    
    def cleanup_unused_models(self, models_to_keep: list):
        """Remove models not in the keep list"""
        for key in list(self._models.keys()):
            if key not in models_to_keep:
                logger.info(f"Removing unused model: {key}")
                del self._models[key]
    
    def get_model_stats(self) -> Dict[str, Any]:
        """Get statistics about loaded models"""
        stats = {
            "loaded_models": list(self._models.keys()),
            "model_count": len(self._models),
            "memory_usage_estimate": self._estimate_memory_usage()
        }
        return stats
    
    def _estimate_memory_usage(self) -> str:
        """Estimate memory usage of loaded models"""
        # Rough estimates based on model types
        total_mb = 0
        
        for key, model in self._models.items():
            if "whisper_base" in key:
                total_mb += 150
            elif "whisper_tiny" in key:
                total_mb += 40
            elif "spacy" in key:
                total_mb += 40
            elif "claude" in key:
                total_mb += 10  # API client only
            else:
                total_mb += 50  # Default estimate
        
        return f"{total_mb} MB"

# Global instance
model_manager = CentralizedModelManager()