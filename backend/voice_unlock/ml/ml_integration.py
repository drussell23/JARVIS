"""
ML Integration Module for Voice Unlock
=====================================

Integrates all ML components with memory optimization,
performance monitoring, and smart resource management.
"""

import os
import sys
import logging
from typing import Dict, Any, Optional, List
import numpy as np
from pathlib import Path
from datetime import datetime
import json

# Add parent directory to path for imports
sys.path.append(str(Path(__file__).parent.parent))

from .ml_manager import get_ml_manager
from .optimized_voice_auth import OptimizedVoiceAuthenticator
from .performance_monitor import get_monitor
from ..config import get_config

logger = logging.getLogger(__name__)


class VoiceUnlockMLSystem:
    """
    Complete ML system for voice unlock with optimizations
    """
    
    def __init__(self):
        self.config = get_config()
        
        # Initialize components
        self.authenticator = OptimizedVoiceAuthenticator()
        self.ml_manager = get_ml_manager()
        self.monitor = get_monitor({
            'thresholds': {
                'memory_usage_mb': self.config.performance.max_memory_mb * 0.8,
                'cpu_percent': self.config.performance.max_cpu_percent,
                'inference_time_ms': 100,
                'error_rate': 0.1,
                'cache_hit_rate': 0.7
            }
        })
        
        # Set up alert handling
        self.monitor.add_alert_callback(self._handle_performance_alert)
        
        # System state
        self.is_healthy = True
        self.degraded_mode = False
        
        logger.info("Voice Unlock ML System initialized")
        
    def enroll_user(self, user_id: str, audio_samples: List[np.ndarray], 
                    sample_rate: int = 16000) -> Dict[str, Any]:
        """
        Enroll user with performance tracking
        """
        start_time = datetime.now()
        
        try:
            # Check system health
            if not self._check_system_health():
                return {
                    'success': False,
                    'error': 'System overloaded, please try again later'
                }
                
            # Perform enrollment
            success = self.authenticator.enroll_user(user_id, audio_samples, sample_rate)
            
            # Track performance
            processing_time = (datetime.now() - start_time).total_seconds()
            self.monitor.record_enrollment(
                user_id, success, len(audio_samples), processing_time
            )
            
            # Get memory stats
            ml_stats = self.ml_manager.get_performance_report()
            
            return {
                'success': success,
                'processing_time': processing_time,
                'memory_used_mb': ml_stats['memory']['ml_memory_mb'],
                'message': 'Enrollment successful' if success else 'Enrollment failed'
            }
            
        except Exception as e:
            logger.error(f"Enrollment error: {e}")
            return {
                'success': False,
                'error': str(e)
            }
            
    def authenticate_user(self, user_id: str, audio_data: np.ndarray, 
                         sample_rate: int = 16000) -> Dict[str, Any]:
        """
        Authenticate user with performance tracking and optimization
        """
        start_time = datetime.now()
        
        try:
            # Check if we're in degraded mode
            if self.degraded_mode:
                logger.warning("Running in degraded mode due to resource constraints")
                
            # Perform authentication
            result = self.authenticator.authenticate(user_id, audio_data, sample_rate)
            
            # Track performance
            processing_time = (datetime.now() - start_time).total_seconds()
            self.monitor.record_authentication(
                user_id, 
                result['authenticated'], 
                result['confidence'],
                processing_time
            )
            
            # Record model inference time
            if 'processing_time' in result:
                model_id = f"voice_model_{user_id}"
                self.monitor.record_inference(
                    model_id,
                    result['processing_time'],
                    result['authenticated']
                )
                
            # Add system info to result
            result['system_health'] = self._get_system_health_status()
            
            return result
            
        except Exception as e:
            logger.error(f"Authentication error: {e}")
            return {
                'authenticated': False,
                'confidence': 0.0,
                'error': str(e),
                'system_health': self._get_system_health_status()
            }
            
    def _check_system_health(self) -> bool:
        """Check if system is healthy for new operations (macOS-aware)"""
        stats = self.monitor.get_current_stats()
        
        # Check memory - use available memory instead of percentage
        memory_available_mb = stats['system'].get('memory_available_mb', 2048)
        available_gb = memory_available_mb / 1024.0
        if available_gb < 0.5:  # Less than 500MB available
            logger.warning(f"Low available memory: {available_gb:.1f}GB")
            self.is_healthy = False
            return False
            
        # Check CPU
        cpu_percent = stats['system']['cpu_percent']
        if cpu_percent > self.config.performance.max_cpu_percent:
            logger.warning(f"High CPU usage: {cpu_percent}%")
            # Don't fail, but note it
            
        # Check ML memory
        ml_memory = stats['system']['ml_memory_mb']
        if ml_memory > self.config.performance.max_memory_mb:
            logger.warning(f"ML memory limit exceeded: {ml_memory}MB")
            # Trigger cleanup
            self._cleanup_resources()
            
        self.is_healthy = True
        return True
        
    def _get_system_health_status(self) -> Dict[str, Any]:
        """Get current system health status"""
        stats = self.monitor.get_current_stats()
        
        return {
            'healthy': self.is_healthy,
            'degraded_mode': self.degraded_mode,
            'memory_percent': stats['system']['memory_percent'],
            'cpu_percent': stats['system']['cpu_percent'],
            'ml_memory_mb': stats['system']['ml_memory_mb'],
            'active_models': stats['models']['active'],
            'cache_size_mb': self.ml_manager.cache.current_memory / 1024 / 1024
        }
        
    def _handle_performance_alert(self, alert: Dict[str, Any]):
        """Handle performance alerts from monitor"""
        logger.warning(f"Performance alert: {alert}")
        
        if alert['metric'] == 'ml_memory_mb' and alert['severity'] == 'critical':
            # Enter degraded mode
            self.degraded_mode = True
            self._cleanup_resources()
            
        elif alert['metric'] == 'cpu_percent' and alert['severity'] == 'critical':
            # Reduce processing
            self.degraded_mode = True
            
        elif alert['metric'] == 'cache_hit_rate':
            # Cache performance issue
            logger.info("Low cache hit rate detected")
            
    def _cleanup_resources(self):
        """Clean up resources to free memory"""
        logger.info("Cleaning up resources...")
        
        # Unload unused models
        self.ml_manager.unload_unused_models(timeout_seconds=60)
        
        # Clear old cache entries
        if self.ml_manager.cache.current_memory > 100 * 1024 * 1024:  # 100MB
            self.ml_manager.cache.clear()
            
        # Force garbage collection
        import gc
        gc.collect()
        
        # Exit degraded mode if memory is OK now
        stats = self.monitor.get_current_stats()
        if stats['system']['memory_percent'] < 70:
            self.degraded_mode = False
            logger.info("Exited degraded mode")
            
    def get_performance_report(self) -> Dict[str, Any]:
        """Get comprehensive performance report"""
        ml_report = self.ml_manager.get_performance_report()
        monitor_stats = self.monitor.get_current_stats()
        
        return {
            'timestamp': datetime.now().isoformat(),
            'system_health': self._get_system_health_status(),
            'ml_performance': ml_report,
            'system_stats': monitor_stats,
            'recommendations': self._get_optimization_recommendations()
        }
        
    def _get_optimization_recommendations(self) -> List[str]:
        """Get optimization recommendations based on current performance"""
        recommendations = []
        stats = self.monitor.get_current_stats()
        
        # Memory recommendations
        if stats['system']['memory_percent'] > 80:
            recommendations.append("Consider increasing system RAM or reducing max_memory_mb setting")
            
        if stats['system']['ml_memory_mb'] > 300:
            recommendations.append("Enable model quantization to reduce memory usage")
            
        # Cache recommendations
        cache_hit_rate = self.ml_manager._get_cache_hit_rate()
        if cache_hit_rate < 70:
            recommendations.append("Increase cache_size_mb to improve performance")
            
        # Model recommendations
        if stats['models']['avg_inference_time_ms'] > 100:
            recommendations.append("Consider using lighter models or enabling GPU acceleration")
            
        # Authentication recommendations
        if stats['authentication']['success_rate'] < 0.8:
            recommendations.append("Review authentication thresholds or improve enrollment quality")
            
        return recommendations
        
    def export_diagnostics(self, output_path: str):
        """Export complete diagnostics for troubleshooting"""
        diagnostics = {
            'timestamp': datetime.now().isoformat(),
            'config': self.config.to_dict(),
            'performance_report': self.get_performance_report(),
            'model_inventory': {},
            'recent_authentications': [],
            'system_info': {
                'platform': sys.platform,
                'python_version': sys.version,
                'total_ram_gb': psutil.virtual_memory().total / 1024 / 1024 / 1024
            }
        }
        
        # Add model information
        for user_id, user_model in self.authenticator.user_models.items():
            diagnostics['model_inventory'][user_id] = {
                'model_id': user_model.model_id,
                'created': user_model.created.isoformat(),
                'sample_count': user_model.sample_count,
                'model_size_mb': user_model.model_path.stat().st_size / 1024 / 1024 if user_model.model_path.exists() else 0
            }
            
        # Export to file
        with open(output_path, 'w') as f:
            json.dump(diagnostics, f, indent=2)
            
        logger.info(f"Diagnostics exported to {output_path}")
        
    def cleanup(self):
        """Clean up all resources"""
        logger.info("Shutting down ML system...")
        
        # Stop monitoring
        self.monitor.stop_monitoring()
        
        # Export final report
        try:
            report_path = Path(self.config.security.audit_path).parent / 'final_report.json'
            self.monitor.export_report(str(report_path))
        except Exception:
            pass
            
        # Clean up ML manager
        self.ml_manager.cleanup()
        
        # Clean up authenticator
        self.authenticator.cleanup()
        
        logger.info("ML system shutdown complete")


# Example usage and testing
def test_ml_integration():
    """Test the integrated ML system"""
    import sounddevice as sd
    
    # Initialize system
    ml_system = VoiceUnlockMLSystem()
    
    # Show initial status
    print("Initial system status:")
    print(json.dumps(ml_system.get_performance_report(), indent=2))
    
    # Test enrollment
    print("\nTesting enrollment...")
    duration = 3
    sample_rate = 16000
    
    audio_samples = []
    for i in range(3):
        print(f"Recording sample {i+1}/3 (3 seconds)...")
        audio = sd.rec(int(duration * sample_rate), 
                      samplerate=sample_rate, channels=1, dtype='float32')
        sd.wait()
        audio_samples.append(audio.flatten())
        
    result = ml_system.enroll_user("test_user", audio_samples, sample_rate)
    print(f"Enrollment result: {json.dumps(result, indent=2)}")
    
    # Test authentication
    print("\nTesting authentication...")
    print("Recording 3 seconds for authentication...")
    test_audio = sd.rec(int(duration * sample_rate), 
                       samplerate=sample_rate, channels=1, dtype='float32')
    sd.wait()
    
    auth_result = ml_system.authenticate_user("test_user", test_audio.flatten(), sample_rate)
    print(f"Authentication result: {json.dumps(auth_result, indent=2)}")
    
    # Show performance after operations
    print("\nPerformance report after operations:")
    print(json.dumps(ml_system.get_performance_report(), indent=2))
    
    # Export diagnostics
    ml_system.export_diagnostics("ml_diagnostics.json")
    
    # Cleanup
    ml_system.cleanup()


if __name__ == "__main__":
    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    test_ml_integration()