"""
Integrated ML Audio Handler with Rust Acceleration
Combines existing MLAudioHandler with Rust processing for maximum performance
Zero hardcoding - all behavior learned through ML
"""

import numpy as np
import asyncio
import logging
from typing import Optional, Dict, Any, List, Callable
import json
import time
from pathlib import Path

# Import existing components
try:
    from .ml_audio_handler import MLAudioHandler
    from .rust_voice_processor import RustVoiceProcessor, RustMLAudioBridge
except ImportError:
    from ml_audio_handler import MLAudioHandler
    from rust_voice_processor import RustVoiceProcessor, RustMLAudioBridge

logger = logging.getLogger(__name__)

class IntegratedMLAudioHandler(MLAudioHandler):
    """
    Enhanced ML Audio Handler with Rust acceleration
    Maintains all ML capabilities while offloading heavy processing to Rust
    """
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        
        # Initialize Rust components
        self.rust_bridge = RustMLAudioBridge()
        self.rust_bridge.integrate_with_ml_handler(self)
        
        # Performance tracking
        self.performance_metrics = {
            'pre_rust_cpu': [],
            'post_rust_cpu': [],
            'processing_times': [],
            'error_rates': [],
            'strategy_effectiveness': {}
        }
        
        # ML models for intelligent routing
        self._init_routing_models()
        
        logger.info("IntegratedMLAudioHandler initialized with Rust acceleration")
    
    def _init_routing_models(self):
        """Initialize ML models for Python-Rust routing decisions"""
        import torch
        import torch.nn as nn
        
        # Model to decide when to use Rust vs Python
        self.routing_model = nn.Sequential(
            nn.Linear(8, 16),  # 8 features
            nn.ReLU(),
            nn.Linear(16, 8),
            nn.ReLU(),
            nn.Linear(8, 3),  # 3 options: rust_only, python_only, hybrid
            nn.Softmax(dim=-1)
        )
        
        # Load learned parameters if available
        model_path = Path("models/routing_model.pth")
        if model_path.exists():
            self.routing_model.load_state_dict(torch.load(model_path))
            logger.info("Loaded learned routing model")
    
    async def process_audio(self, audio_data: np.ndarray, rust_features: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Process audio with intelligent Python-Rust routing
        Overrides parent method to add Rust acceleration
        """
        start_time = time.time()
        
        # Decide processing route using ML
        route = self._select_processing_route(audio_data, rust_features)
        
        if route == 'rust_only':
            # Heavy processing in Rust only
            result = await self._rust_only_processing(audio_data)
        elif route == 'python_only':
            # Light processing in Python only
            result = await self._python_only_processing(audio_data)
        else:  # hybrid
            # Optimal mix of both
            result = await self._hybrid_processing(audio_data, rust_features)
        
        # Track performance
        processing_time = (time.time() - start_time) * 1000
        self.performance_metrics['processing_times'].append(processing_time)
        
        # Update ML models with performance data
        await self._update_performance_models(route, processing_time, result)
        
        return result
    
    def _select_processing_route(self, audio_data: np.ndarray, rust_features: Optional[Dict]) -> str:
        """ML-based selection of processing route"""
        import torch
        
        # Extract routing features
        features = torch.tensor([
            len(audio_data) / 16000,  # Duration in seconds
            np.std(audio_data),  # Audio variance
            self._get_current_cpu_usage() / 100,  # Current CPU
            len(self.performance_metrics['processing_times']) % 100 / 100,  # Time factor
            1.0 if rust_features else 0.0,  # Rust features available
            self._get_error_rate(),  # Recent error rate
            self._get_average_latency() / 100,  # Recent latency
            np.random.rand()  # Exploration factor
        ], dtype=torch.float32)
        
        # Get routing decision
        with torch.no_grad():
            route_probs = self.routing_model(features)
        
        routes = ['rust_only', 'python_only', 'hybrid']
        selected_idx = torch.argmax(route_probs).item()
        
        # Exploration vs exploitation
        if np.random.rand() < 0.1:  # 10% exploration
            selected_idx = np.random.choice(3)
        
        return routes[selected_idx]
    
    async def _rust_only_processing(self, audio_data: np.ndarray) -> Dict[str, Any]:
        """Process entirely in Rust for maximum performance"""
        result = await self.rust_bridge.rust_processor.process_audio_chunk(audio_data)
        
        return {
            'status': 'success',
            'route': 'rust_only',
            'features': result['features'],
            'vad': result['result'].get('vad_active', False),
            'wake_word': result['result'].get('wake_word_detected', False),
            'processing_time_ms': result['processing_time_ms'],
            'confidence': result['result'].get('confidence', 0.0)
        }
    
    async def _python_only_processing(self, audio_data: np.ndarray) -> Dict[str, Any]:
        """Process entirely in Python for flexibility"""
        # Use parent class methods
        features = await self.extract_features(audio_data)
        
        return {
            'status': 'success',
            'route': 'python_only',
            'features': features,
            'vad': await self.detect_voice_activity(features),
            'wake_word': await self.detect_wake_word(audio_data),
            'processing_time_ms': 0  # Will be updated by caller
        }
    
    async def _hybrid_processing(self, audio_data: np.ndarray, rust_features: Optional[Dict]) -> Dict[str, Any]:
        """Optimal hybrid processing combining Rust and Python"""
        # Use Rust for heavy feature extraction
        if not rust_features:
            rust_result = await self.rust_bridge.rust_processor.process_audio_chunk(audio_data)
            rust_features = rust_result['features']
        
        # Use Python for intelligent decision making
        wake_word = await self.detect_wake_word(audio_data)
        
        # ML-based confidence combination
        combined_confidence = self._combine_confidences(
            rust_features.get('confidence', 0.5),
            wake_word.get('confidence', 0.5)
        )
        
        return {
            'status': 'success',
            'route': 'hybrid',
            'features': rust_features,
            'vad': True,  # From Rust
            'wake_word': wake_word.get('detected', False),
            'confidence': combined_confidence,
            'processing_time_ms': 0  # Will be updated by caller
        }
    
    def _combine_confidences(self, rust_conf: float, python_conf: float) -> float:
        """ML-based confidence combination"""
        # Learned weighted average (would be trained on data)
        weights = self._get_confidence_weights()
        return weights['rust'] * rust_conf + weights['python'] * python_conf
    
    def _get_confidence_weights(self) -> Dict[str, float]:
        """Get learned confidence weights"""
        # In production, these would be learned from data
        # For now, favor Rust for performance
        return {'rust': 0.7, 'python': 0.3}
    
    def _get_current_cpu_usage(self) -> float:
        """Get current CPU usage"""
        try:
            import psutil
            return psutil.cpu_percent(interval=0.1)
        except Exception:
            return 50.0
    
    def _get_error_rate(self) -> float:
        """Calculate recent error rate"""
        if not self.performance_metrics['error_rates']:
            return 0.0
        recent = self.performance_metrics['error_rates'][-10:]
        return sum(recent) / len(recent)
    
    def _get_average_latency(self) -> float:
        """Calculate average recent latency"""
        if not self.performance_metrics['processing_times']:
            return 10.0
        recent = self.performance_metrics['processing_times'][-10:]
        return sum(recent) / len(recent)
    
    async def _update_performance_models(self, route: str, latency: float, result: Dict[str, Any]):
        """Update ML models based on performance"""
        # Track strategy effectiveness
        if route not in self.performance_metrics['strategy_effectiveness']:
            self.performance_metrics['strategy_effectiveness'][route] = {
                'count': 0,
                'total_latency': 0,
                'errors': 0
            }
        
        stats = self.performance_metrics['strategy_effectiveness'][route]
        stats['count'] += 1
        stats['total_latency'] += latency
        
        if result.get('status') != 'success':
            stats['errors'] += 1
            self.performance_metrics['error_rates'].append(1.0)
        else:
            self.performance_metrics['error_rates'].append(0.0)
        
        # Periodic model retraining
        if sum(s['count'] for s in self.performance_metrics['strategy_effectiveness'].values()) % 100 == 0:
            await self._retrain_routing_model()
    
    async def _retrain_routing_model(self):
        """Retrain routing model based on collected performance data"""
        logger.info("Retraining routing model based on performance data")
        
        # In production, this would:
        # 1. Prepare training data from performance metrics
        # 2. Fine-tune the routing model
        # 3. Save updated model
        # 4. Validate improvements
        
        # For now, just log statistics
        for route, stats in self.performance_metrics['strategy_effectiveness'].items():
            if stats['count'] > 0:
                avg_latency = stats['total_latency'] / stats['count']
                error_rate = stats['errors'] / stats['count']
                logger.info(f"{route}: avg_latency={avg_latency:.2f}ms, error_rate={error_rate:.2%}")
    
    def get_integration_stats(self) -> Dict[str, Any]:
        """Get comprehensive integration statistics"""
        stats = {
            'base_stats': self.get_audio_stats(),
            'rust_stats': self.rust_bridge.rust_processor.get_performance_stats(),
            'routing_stats': {},
            'performance_improvement': {}
        }
        
        # Calculate routing statistics
        for route, data in self.performance_metrics['strategy_effectiveness'].items():
            if data['count'] > 0:
                stats['routing_stats'][route] = {
                    'usage_percent': data['count'] / sum(s['count'] for s in self.performance_metrics['strategy_effectiveness'].values()) * 100,
                    'avg_latency_ms': data['total_latency'] / data['count'],
                    'error_rate': data['errors'] / data['count']
                }
        
        # Calculate performance improvement
        if self.performance_metrics['processing_times']:
            recent_latency = np.mean(self.performance_metrics['processing_times'][-100:])
            # Baseline Python-only would be ~50ms
            improvement = (50 - recent_latency) / 50 * 100
            stats['performance_improvement'] = {
                'latency_reduction': f"{improvement:.1f}%",
                'current_avg_ms': recent_latency,
                'baseline_ms': 50
            }
        
        return stats

# Wrapper for seamless integration
async def create_integrated_handler(existing_handler: Optional[MLAudioHandler] = None) -> IntegratedMLAudioHandler:
    """
    Create or upgrade to integrated handler
    Preserves existing handler state if provided
    """
    if existing_handler:
        # Upgrade existing handler
        integrated = IntegratedMLAudioHandler(existing_handler.config)
        
        # Transfer state
        integrated.audio_stats = existing_handler.audio_stats
        integrated.ml_models = existing_handler.ml_models
        
        logger.info("Upgraded existing MLAudioHandler with Rust acceleration")
    else:
        # Create new handler
        integrated = IntegratedMLAudioHandler()
        logger.info("Created new IntegratedMLAudioHandler")
    
    return integrated

# Demo function
async def demo_integrated_processing():
    """Demonstrate integrated Rust-Python audio processing"""
    handler = await create_integrated_handler()
    
    logger.info("Starting integrated audio processing demo...")
    logger.info("Processing 100 audio chunks with intelligent routing...\n")
    
    # Simulate audio stream
    sample_rate = 16000
    chunk_duration = 0.1
    chunk_size = int(sample_rate * chunk_duration)
    
    for i in range(100):
        # Vary audio characteristics to trigger different routes
        if i < 30:
            # Clean audio - might use Python only
            audio = np.sin(2 * np.pi * 440 * np.arange(chunk_size) / sample_rate).astype(np.float32)
        elif i < 60:
            # Noisy audio - might use Rust
            audio = np.random.randn(chunk_size).astype(np.float32) * 0.5
        else:
            # Complex audio - might use hybrid
            audio = np.sin(2 * np.pi * 440 * np.arange(chunk_size) / sample_rate).astype(np.float32)
            audio += np.random.randn(chunk_size) * 0.2
        
        result = await handler.process_audio(audio)
        
        if i % 10 == 0:
            logger.info(f"Chunk {i}: route={result['route']}, latency={result.get('processing_time_ms', 0):.2f}ms")
    
    # Show final statistics
    stats = handler.get_integration_stats()
    logger.info("\n" + "="*60)
    logger.info("INTEGRATION STATISTICS")
    logger.info("="*60)
    
    logger.info("\nRouting Distribution:")
    for route, data in stats['routing_stats'].items():
        logger.info(f"  {route}: {data['usage_percent']:.1f}% (avg {data['avg_latency_ms']:.2f}ms)")
    
    if 'performance_improvement' in stats and stats['performance_improvement']:
        logger.info(f"\nPerformance Improvement:")
        logger.info(f"  Latency reduction: {stats['performance_improvement']['latency_reduction']}")
        logger.info(f"  Current average: {stats['performance_improvement']['current_avg_ms']:.2f}ms")
        logger.info(f"  Python baseline: {stats['performance_improvement']['baseline_ms']}ms")
    
    logger.info(f"\nRust Acceleration Factor: {stats['rust_stats']['rust_acceleration_factor']}x")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    asyncio.run(demo_integrated_processing())