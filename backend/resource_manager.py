#!/usr/bin/env python3
"""
JARVIS Resource Manager
======================

Strict resource management for 16GB MacBook systems.
Ensures JARVIS stays within 70% total system memory usage.
"""

import psutil
import threading
import time
import logging
from typing import Dict, Any, Optional, List, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from collections import deque, OrderedDict
from enum import Enum
import os
import signal
import numpy as np

logger = logging.getLogger(__name__)


class ServicePriority(Enum):
    """Service priorities for extreme memory management"""
    CRITICAL = 0     # Apple Watch proximity detection
    HIGH = 1         # Voice authentication when triggered
    MEDIUM = 2       # Voice command processing
    LOW = 3          # Background services
    IDLE = 4         # Can be unloaded anytime


@dataclass
class ResourceSnapshot:
    """Point-in-time resource usage"""
    timestamp: datetime
    memory_percent: float
    memory_available_mb: float
    memory_used_mb: float
    cpu_percent: float
    cpu_per_core: List[float]
    jarvis_memory_mb: float
    ml_models_loaded: int
    active_services: List[str]


class ResourceManager:
    """
    Strict resource management for JARVIS on 16GB systems
    
    Goals:
    - Keep total system memory usage below 70% (11.2GB on 16GB system)
    - Throttle CPU usage to prevent thermal issues
    - Load only ONE ML model at a time
    - Predictive loading based on usage patterns
    """
    
    def __init__(self):
        # Ultra-aggressive constraints for 16GB MacBook (30% target)
        self.TOTAL_RAM_GB = 16
        self.MAX_MEMORY_PERCENT = 30.0  # Ultra-aggressive: 30% total system usage (4.8GB)
        self.PANIC_MEMORY_PERCENT = 35.0  # Panic mode above 35%
        self.MAX_JARVIS_MEMORY_MB = 1024  # 1GB hard limit for JARVIS (reduced from 2GB)
        self.MAX_ML_MEMORY_MB = 300  # 300MB for ML models (reduced from 400MB)
        self.MAX_CPU_PERCENT = 40.0  # Throttle above 40% CPU (more aggressive)
        
        # Current state
        self.current_ml_model: Optional[str] = None
        self.active_services: Dict[str, bool] = {
            'voice_unlock': False,
            'vision': False,
            'cleanup': False,
            'ml_inference': False
        }
        
        # Service priorities for ultra-aggressive management
        self.service_priorities = {
            'proximity_detection': ServicePriority.CRITICAL,
            'voice_capture': ServicePriority.HIGH,
            'voice_auth': ServicePriority.HIGH,
            'ml_inference': ServicePriority.MEDIUM,
            'vision': ServicePriority.LOW,
            'cleanup': ServicePriority.IDLE
        }
        
        # Proximity + Voice state
        self.proximity_active = False
        self.voice_unlock_pending = False
        self.last_proximity_check = datetime.now()
        
        # Resource tracking
        self.history = deque(maxlen=100)  # Last 100 snapshots
        self.ml_model_queue = deque(maxlen=10)  # Predictive queue
        
        # Throttling state - more aggressive levels for 30% target
        self.throttle_level = 0  # 0=none, 1=mild, 2=moderate, 3=severe, 4=extreme, 5=critical
        self.last_throttle_change = datetime.now()
        
        # Advanced prediction for ultra-aggressive management
        self.usage_patterns = deque(maxlen=1000)
        self.prediction_enabled = True
        
        # Callbacks for resource actions
        self.unload_callbacks: Dict[str, Callable] = {}
        self.throttle_callbacks: List[Callable] = []
        
        # Monitoring
        self.monitoring = False
        self.monitor_thread = None
        
        logger.info(f"Resource Manager initialized for {self.TOTAL_RAM_GB}GB system")
        
    def start_monitoring(self):
        """Start resource monitoring"""
        if self.monitoring:
            return
            
        self.monitoring = True
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()
        logger.info("Resource monitoring started")
        
    def stop_monitoring(self):
        """Stop resource monitoring"""
        self.monitoring = False
        if self.monitor_thread:
            self.monitor_thread.join(timeout=2)
        logger.info("Resource monitoring stopped")
        
    def _monitor_loop(self):
        """Main monitoring loop"""
        while self.monitoring:
            try:
                snapshot = self._take_snapshot()
                self.history.append(snapshot)
                
                # Check if we need to take action
                self._check_memory_pressure(snapshot)
                self._check_cpu_pressure(snapshot)
                
                # Ultra-aggressive monitoring intervals for 30% target
                if memory.percent > 28:  # Near 30% limit
                    time.sleep(0.2)  # Check every 200ms
                elif self.throttle_level > 3:
                    time.sleep(0.3)  # Check every 300ms under extreme pressure
                elif self.throttle_level > 2:
                    time.sleep(0.5)  # Check more frequently under pressure
                else:
                    time.sleep(1.0)  # Normal interval (reduced from 2.0)
                    
            except Exception as e:
                logger.error(f"Monitor loop error: {e}")
                time.sleep(5)
                
    def _take_snapshot(self) -> ResourceSnapshot:
        """Take a resource usage snapshot"""
        memory = psutil.virtual_memory()
        cpu_percent = psutil.cpu_percent(interval=0.1)
        cpu_per_core = psutil.cpu_percent(interval=0.1, percpu=True)
        
        # Get JARVIS process memory
        try:
            process = psutil.Process()
            jarvis_memory = process.memory_info().rss / (1024 * 1024)  # MB
        except Exception:
            jarvis_memory = 0
            
        # Count loaded ML models
        ml_models = 0
        if self.current_ml_model:
            ml_models = 1
            
        return ResourceSnapshot(
            timestamp=datetime.now(),
            memory_percent=memory.percent,
            memory_available_mb=memory.available / (1024 * 1024),
            memory_used_mb=memory.used / (1024 * 1024),
            cpu_percent=cpu_percent,
            cpu_per_core=cpu_per_core,
            jarvis_memory_mb=jarvis_memory,
            ml_models_loaded=ml_models,
            active_services=[k for k, v in self.active_services.items() if v]
        )
        
    def _check_memory_pressure(self, snapshot: ResourceSnapshot):
        """Check memory pressure and take action"""
        if snapshot.memory_percent > self.MAX_MEMORY_PERCENT:
            logger.warning(f"High memory usage: {snapshot.memory_percent:.1f}%")
            
            # Calculate how much we need to free
            target_mb = (snapshot.memory_percent - self.MAX_MEMORY_PERCENT) * \
                       self.TOTAL_RAM_GB * 1024 / 100
                       
            self._free_memory(target_mb, snapshot)
            
    def _check_cpu_pressure(self, snapshot: ResourceSnapshot):
        """Check CPU pressure and adjust throttling"""
        if snapshot.cpu_percent > self.MAX_CPU_PERCENT:
            self._increase_throttle()
        elif snapshot.cpu_percent < self.MAX_CPU_PERCENT * 0.7:  # 35%
            self._decrease_throttle()
            
    def _free_memory(self, target_mb: float, snapshot: ResourceSnapshot):
        """Free memory to meet target"""
        freed_mb = 0
        
        # Priority order for unloading
        unload_order = [
            ('ml_model', self.MAX_ML_MEMORY_MB),
            ('vision', 200),
            ('cleanup', 100),
            ('voice_unlock_cache', 150)
        ]
        
        for component, estimated_mb in unload_order:
            if freed_mb >= target_mb:
                break
                
            if component == 'ml_model' and self.current_ml_model:
                logger.info(f"Unloading ML model: {self.current_ml_model}")
                if 'ml_model' in self.unload_callbacks:
                    self.unload_callbacks['ml_model']()
                self.current_ml_model = None
                freed_mb += estimated_mb
                
            elif component in self.unload_callbacks and self.active_services.get(component, False):
                logger.info(f"Unloading service: {component}")
                self.unload_callbacks[component]()
                self.active_services[component] = False
                freed_mb += estimated_mb
                
        logger.info(f"Freed approximately {freed_mb:.1f}MB")
        
    def _increase_throttle(self):
        """Increase throttling level"""
        if self.throttle_level < 3:
            self.throttle_level += 1
            logger.info(f"Increased throttle level to {self.throttle_level}")
            self._notify_throttle_change()
            
    def _decrease_throttle(self):
        """Decrease throttling level"""
        # Only decrease if we've been stable for 30 seconds
        if (datetime.now() - self.last_throttle_change).seconds < 30:
            return
            
        if self.throttle_level > 0:
            self.throttle_level -= 1
            logger.info(f"Decreased throttle level to {self.throttle_level}")
            self._notify_throttle_change()
            
    def _notify_throttle_change(self):
        """Notify callbacks about throttle change"""
        self.last_throttle_change = datetime.now()
        for callback in self.throttle_callbacks:
            try:
                callback(self.throttle_level)
            except Exception as e:
                logger.error(f"Throttle callback error: {e}")
                
    def request_voice_unlock_resources(self) -> bool:
        """
        Special method for Proximity + Voice Unlock scenario.
        Pre-allocates resources for the complete flow.
        """
        logger.info("Voice unlock resource request - preparing for proximity + voice")
        
        # Check current memory
        snapshot = self._take_snapshot()
        
        # For 30% target, we need to be VERY careful
        if snapshot.memory_percent > 28:  # Leave 2% buffer
            logger.warning(f"Memory at {snapshot.memory_percent:.1f}% - aggressive cleanup needed")
            
            # Ultra-aggressive cleanup for voice unlock
            self._emergency_cleanup_for_voice_unlock()
            
            # Re-check after cleanup
            new_snapshot = self._take_snapshot()
            if new_snapshot.memory_percent > 28:
                logger.error("Cannot allocate resources for voice unlock")
                return False
        
        # Reserve resources
        self.voice_unlock_pending = True
        self.proximity_active = True
        
        # Ensure critical services are prioritized
        self._prioritize_voice_unlock_services()
        
        return True
    
    def _prioritize_voice_unlock_services(self):
        """Prioritize services for voice unlock scenario"""
        # Mark voice-related services as critical
        priority_overrides = {
            'proximity_detection': ServicePriority.CRITICAL,
            'voice_capture': ServicePriority.CRITICAL,
            'voice_auth': ServicePriority.CRITICAL,
            'vision': ServicePriority.IDLE,  # Downgrade non-essential
            'cleanup': ServicePriority.IDLE
        }
        
        self.service_priorities.update(priority_overrides)
        logger.info("Voice unlock services prioritized")
    
    def _emergency_cleanup_for_voice_unlock(self):
        """Emergency cleanup specifically for voice unlock"""
        # Unload everything except critical services
        for service, active in list(self.active_services.items()):
            if active and service not in ['proximity_detection', 'voice_capture']:
                if service in self.unload_callbacks:
                    self.unload_callbacks[service]()
                    self.active_services[service] = False
                    logger.info(f"Unloaded {service} for voice unlock")
        
        # Clear all ML models
        if self.current_ml_model:
            if 'ml_model' in self.unload_callbacks:
                self.unload_callbacks['ml_model']()
            self.current_ml_model = None
        
        # Force garbage collection
        import gc
        gc.collect()
    
    def request_ml_model(self, model_id: str, priority: int = 5) -> bool:
        """
        Request to load an ML model
        
        Args:
            model_id: Model identifier
            priority: 1-10 (10 = highest priority)
            
        Returns:
            True if model can be loaded, False if denied
        """
        snapshot = self._take_snapshot()
        
        # Ultra-strict check for 30% target
        if snapshot.memory_percent > 25:  # Only 25% to leave 5% buffer for 30% target
            logger.warning(f"ML model request denied: memory at {snapshot.memory_percent:.1f}% (limit: 25%)")
            return False
            
        # Check if another model is loaded
        if self.current_ml_model and self.current_ml_model != model_id:
            logger.info(f"Unloading current model: {self.current_ml_model}")
            if 'ml_model' in self.unload_callbacks:
                self.unload_callbacks['ml_model']()
                
        # Track for prediction
        self.ml_model_queue.append((model_id, datetime.now()))
        
        self.current_ml_model = model_id
        logger.info(f"ML model approved: {model_id}")
        return True
        
    def predict_next_model(self) -> Optional[str]:
        """Predict next likely ML model based on patterns"""
        if len(self.ml_model_queue) < 3:
            return None
            
        # Simple frequency-based prediction
        model_counts = {}
        for model_id, timestamp in self.ml_model_queue:
            # Weight recent accesses more
            age = (datetime.now() - timestamp).seconds
            weight = 1.0 if age < 60 else 0.5 if age < 300 else 0.1
            model_counts[model_id] = model_counts.get(model_id, 0) + weight
            
        if model_counts:
            return max(model_counts, key=model_counts.get)
        return None
        
    def get_throttle_delay(self) -> float:
        """Get recommended delay based on throttle level - ultra-aggressive for 30% target"""
        delays = {
            0: 0.0,      # No throttle
            1: 0.05,     # 50ms delay
            2: 0.2,      # 200ms delay
            3: 0.5,      # 500ms delay
            4: 1.0,      # 1 second delay
            5: 2.0       # 2 seconds - critical
        }
        return delays.get(self.throttle_level, 0.0)
        
    def register_unload_callback(self, component: str, callback: Callable):
        """Register callback for component unloading"""
        self.unload_callbacks[component] = callback
        
    def register_throttle_callback(self, callback: Callable):
        """Register callback for throttle changes"""
        self.throttle_callbacks.append(callback)
        
    def get_status(self) -> Dict[str, Any]:
        """Get current resource status"""
        if self.history:
            latest = self.history[-1]
            return {
                'memory_percent': latest.memory_percent,
                'memory_available_mb': latest.memory_available_mb,
                'cpu_percent': latest.cpu_percent,
                'throttle_level': self.throttle_level,
                'current_ml_model': self.current_ml_model,
                'active_services': self.active_services,
                'jarvis_memory_mb': latest.jarvis_memory_mb,
                'prediction': self.predict_next_model()
            }
        return {}
        
    def enforce_memory_limit(self):
        """Enforce hard memory limits (emergency measure)"""
        snapshot = self._take_snapshot()
        
        # Much lower thresholds for 30% target
        if snapshot.memory_percent > self.PANIC_MEMORY_PERCENT:  # 35%
            logger.critical(f"PANIC: Memory at {snapshot.memory_percent:.1f}% (panic level: {self.PANIC_MEMORY_PERCENT}%)")
            
            # Force unload everything
            for component, callback in self.unload_callbacks.items():
                try:
                    callback()
                    logger.info(f"Force unloaded: {component}")
                except Exception as e:
                    logger.error(f"Failed to unload {component}: {e}")
                    
            # Force garbage collection
            import gc
            gc.collect()
            
            # If still critical, consider process restart (macOS-aware)
            new_snapshot = self._take_snapshot()
            available_gb = new_snapshot.memory_available_mb / 1024.0
            if available_gb < 0.5:  # Less than 500MB available
                logger.critical(f"Memory critical ({available_gb:.1f}GB available) - recommend JARVIS restart")
                

# Global instance
_resource_manager: Optional[ResourceManager] = None

def get_resource_manager() -> ResourceManager:
    """Get or create resource manager instance"""
    global _resource_manager
    if _resource_manager is None:
        _resource_manager = ResourceManager()
        _resource_manager.start_monitoring()
    return _resource_manager


def throttled_operation(func: Callable) -> Callable:
    """Decorator to add throttling to operations"""
    def wrapper(*args, **kwargs):
        rm = get_resource_manager()
        delay = rm.get_throttle_delay()
        
        if delay > 0:
            logger.debug(f"Throttling operation by {delay}s")
            time.sleep(delay)
            
        return func(*args, **kwargs)
    return wrapper


if __name__ == "__main__":
    # Test resource manager
    rm = get_resource_manager()
    
    print("Resource Manager Test")
    print("=" * 50)
    
    # Simulate high memory callback
    def unload_ml():
        print("  -> Unloading ML models")
        
    def unload_cache():
        print("  -> Clearing caches")
        
    rm.register_unload_callback('ml_model', unload_ml)
    rm.register_unload_callback('cache', unload_cache)
    
    # Monitor for 10 seconds
    print("\nMonitoring resources for 10 seconds...")
    for i in range(10):
        status = rm.get_status()
        print(f"\n[{i}s] Memory: {status.get('memory_percent', 0):.1f}%, "
              f"CPU: {status.get('cpu_percent', 0):.1f}%, "
              f"Throttle: {status.get('throttle_level', 0)}")
        time.sleep(1)
        
    rm.stop_monitoring()
    print("\nTest complete!")