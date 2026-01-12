"""
Dynamic Resource Monitor for JARVIS
Real-time monitoring and adaptive resource management
"""

import os
import time
import psutil
import threading
import logging
from typing import Dict, List, Callable, Optional, Any
from dataclasses import dataclass, field
from collections import deque
from datetime import datetime, timedelta
import json

from .optimization_config import OptimizationConfig, OPTIMIZATION_CONFIG

logger = logging.getLogger(__name__)

@dataclass
class ResourceSnapshot:
    """Single snapshot of system resources"""
    timestamp: datetime
    cpu_percent: float
    memory_percent: float
    memory_used_mb: float
    memory_available_mb: float
    swap_percent: float
    process_memory_mb: float
    process_cpu_percent: float
    thread_count: int
    open_files: int
    
    # macOS specific
    pressure_level: str = "nominal"  # nominal, warning, critical
    thermal_state: str = "nominal"  # nominal, fair, serious, critical

@dataclass
class ResourceAlert:
    """Resource usage alert"""
    level: str  # info, warning, critical
    resource: str  # cpu, memory, thermal
    message: str
    timestamp: datetime
    value: float
    threshold: float

class ResourceMonitor:
    """
    Monitor system resources and trigger adaptive actions
    """
    
    def __init__(self, 
                 config: OptimizationConfig = None,
                 check_interval: float = 1.0):
        
        self.config = config or OPTIMIZATION_CONFIG
        self.check_interval = check_interval
        
        # Resource tracking
        self.history = deque(maxlen=300)  # 5 minutes at 1s intervals
        self.alerts = deque(maxlen=100)
        
        # Thresholds
        self.thresholds = {
            "cpu_warning": 70.0,
            "cpu_critical": 85.0,
            "memory_warning": self.config.memory.memory_warning_threshold,
            "memory_critical": self.config.memory.memory_critical_threshold,
            "swap_warning": 50.0,
            "process_memory_mb": self.config.memory.max_memory_usage_mb
        }
        
        # Callbacks
        self.alert_callbacks: List[Callable[[ResourceAlert], None]] = []
        self.adaptation_callbacks: List[Callable[[ResourceSnapshot], None]] = []
        
        # Monitoring state
        self.running = False
        self.monitor_thread = None
        self.process = psutil.Process()
        
        # Performance tracking
        self.start_time = time.time()
        self.total_adaptations = 0
        
        logger.info(f"Resource Monitor initialized with {check_interval}s interval")
    
    def start(self):
        """Start resource monitoring"""
        if self.running:
            return
        
        self.running = True
        self.monitor_thread = threading.Thread(
            target=self._monitoring_loop,
            daemon=True
        )
        self.monitor_thread.start()
        logger.info("Resource monitoring started")
    
    def stop(self):
        """Stop resource monitoring"""
        self.running = False
        if self.monitor_thread:
            self.monitor_thread.join(timeout=2.0)
        logger.info("Resource monitoring stopped")
    
    def add_alert_callback(self, callback: Callable[[ResourceAlert], None]):
        """Add callback for resource alerts"""
        self.alert_callbacks.append(callback)
    
    def add_adaptation_callback(self, callback: Callable[[ResourceSnapshot], None]):
        """Add callback for adaptive actions"""
        self.adaptation_callbacks.append(callback)
    
    def get_current_snapshot(self) -> ResourceSnapshot:
        """Get current resource snapshot"""
        try:
            # System resources
            cpu_percent = psutil.cpu_percent(interval=0.1)
            memory = psutil.virtual_memory()
            swap = psutil.swap_memory()
            
            # Process resources
            with self.process.oneshot():
                process_memory = self.process.memory_info()
                process_cpu = self.process.cpu_percent()
                thread_count = self.process.num_threads()
                try:
                    open_files = len(self.process.open_files())
                except Exception:
                    open_files = 0
            
            # macOS specific
            pressure_level = self._get_memory_pressure()
            thermal_state = self._get_thermal_state()
            
            return ResourceSnapshot(
                timestamp=datetime.now(),
                cpu_percent=cpu_percent,
                memory_percent=memory.percent,
                memory_used_mb=memory.used / (1024**2),
                memory_available_mb=memory.available / (1024**2),
                swap_percent=swap.percent,
                process_memory_mb=process_memory.rss / (1024**2),
                process_cpu_percent=process_cpu,
                thread_count=thread_count,
                open_files=open_files,
                pressure_level=pressure_level,
                thermal_state=thermal_state
            )
        except Exception as e:
            logger.error(f"Error getting resource snapshot: {e}")
            return None
    
    def _monitoring_loop(self):
        """Main monitoring loop"""
        while self.running:
            try:
                # Get snapshot
                snapshot = self.get_current_snapshot()
                if snapshot:
                    self.history.append(snapshot)
                    
                    # Check thresholds
                    self._check_thresholds(snapshot)
                    
                    # Trigger adaptations if needed
                    if self.config.enable_adaptive_optimization:
                        self._check_adaptations(snapshot)
                
                # Wait for next check
                time.sleep(self.check_interval)
                
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
    
    def _check_thresholds(self, snapshot: ResourceSnapshot):
        """Check resource thresholds and generate alerts"""
        alerts = []
        
        # CPU alerts
        if snapshot.cpu_percent > self.thresholds["cpu_critical"]:
            alerts.append(ResourceAlert(
                level="critical",
                resource="cpu",
                message=f"CPU usage critical: {snapshot.cpu_percent:.1f}%",
                timestamp=snapshot.timestamp,
                value=snapshot.cpu_percent,
                threshold=self.thresholds["cpu_critical"]
            ))
        elif snapshot.cpu_percent > self.thresholds["cpu_warning"]:
            alerts.append(ResourceAlert(
                level="warning",
                resource="cpu",
                message=f"CPU usage high: {snapshot.cpu_percent:.1f}%",
                timestamp=snapshot.timestamp,
                value=snapshot.cpu_percent,
                threshold=self.thresholds["cpu_warning"]
            ))
        
        # Memory alerts
        if snapshot.memory_percent > self.thresholds["memory_critical"]:
            alerts.append(ResourceAlert(
                level="critical",
                resource="memory",
                message=f"Memory usage critical: {snapshot.memory_percent:.1f}%",
                timestamp=snapshot.timestamp,
                value=snapshot.memory_percent,
                threshold=self.thresholds["memory_critical"]
            ))
        elif snapshot.memory_percent > self.thresholds["memory_warning"]:
            alerts.append(ResourceAlert(
                level="warning",
                resource="memory",
                message=f"Memory usage high: {snapshot.memory_percent:.1f}%",
                timestamp=snapshot.timestamp,
                value=snapshot.memory_percent,
                threshold=self.thresholds["memory_warning"]
            ))
        
        # Process memory alerts
        if snapshot.process_memory_mb > self.thresholds["process_memory_mb"]:
            alerts.append(ResourceAlert(
                level="warning",
                resource="process_memory",
                message=f"Process memory exceeds limit: {snapshot.process_memory_mb:.1f}MB",
                timestamp=snapshot.timestamp,
                value=snapshot.process_memory_mb,
                threshold=self.thresholds["process_memory_mb"]
            ))
        
        # Thermal alerts (macOS)
        if snapshot.thermal_state in ["serious", "critical"]:
            alerts.append(ResourceAlert(
                level="critical" if snapshot.thermal_state == "critical" else "warning",
                resource="thermal",
                message=f"System thermal state: {snapshot.thermal_state}",
                timestamp=snapshot.timestamp,
                value=0,
                threshold=0
            ))
        
        # Process alerts
        for alert in alerts:
            self.alerts.append(alert)
            logger.warning(f"Resource alert: {alert.message}")
            
            # Call callbacks
            for callback in self.alert_callbacks:
                try:
                    callback(alert)
                except Exception as e:
                    logger.error(f"Error in alert callback: {e}")
    
    def _check_adaptations(self, snapshot: ResourceSnapshot):
        """Check if adaptations are needed"""
        # Only adapt every N seconds to avoid thrashing
        if len(self.history) < 10:
            return
        
        # Get recent history
        recent_history = list(self.history)[-10:]
        
        # Check trends
        avg_cpu = sum(s.cpu_percent for s in recent_history) / len(recent_history)
        avg_memory = sum(s.memory_percent for s in recent_history) / len(recent_history)
        
        # Trigger adaptation if consistently high
        should_adapt = (
            avg_cpu > self.thresholds["cpu_warning"] or
            avg_memory > self.thresholds["memory_warning"] or
            snapshot.pressure_level in ["warning", "critical"] or
            snapshot.thermal_state in ["serious", "critical"]
        )
        
        if should_adapt:
            self.total_adaptations += 1
            logger.info(f"Triggering adaptation #{self.total_adaptations}")
            
            # Call adaptation callbacks
            for callback in self.adaptation_callbacks:
                try:
                    callback(snapshot)
                except Exception as e:
                    logger.error(f"Error in adaptation callback: {e}")
    
    def _get_memory_pressure(self) -> str:
        """Get macOS memory pressure level"""
        if not self._is_macos():
            return "nominal"
        
        try:
            # Use vm_stat to get memory pressure
            import subprocess
            result = subprocess.run(
                ['sysctl', 'vm.memory_pressure'],
                capture_output=True,
                text=True
            )
            
            if 'vm.memory_pressure: 1' in result.stdout:
                return "warning"
            elif 'vm.memory_pressure: 2' in result.stdout:
                return "critical"
            else:
                return "nominal"
                
        except Exception:
            return "nominal"

    def _get_thermal_state(self) -> str:
        """Get macOS thermal state"""
        if not self._is_macos():
            return "nominal"
        
        try:
            import subprocess
            result = subprocess.run(
                ['pmset', '-g', 'therm'],
                capture_output=True,
                text=True
            )
            
            if 'CPU_Speed_Limit' in result.stdout:
                if '= 50' in result.stdout:
                    return "serious"
                elif '= 25' in result.stdout:
                    return "critical"
                else:
                    return "fair"
            else:
                return "nominal"
                
        except Exception:
            return "nominal"

    def _is_macos(self) -> bool:
        """Check if running on macOS"""
        import platform
        return platform.system() == "Darwin"
    
    def get_stats(self) -> Dict[str, Any]:
        """Get monitoring statistics"""
        if not self.history:
            return {}
        
        recent = list(self.history)[-60:]  # Last minute
        
        return {
            "uptime_seconds": time.time() - self.start_time,
            "total_adaptations": self.total_adaptations,
            "current_snapshot": self.history[-1].__dict__ if self.history else None,
            "average_cpu_1min": sum(s.cpu_percent for s in recent) / len(recent),
            "average_memory_1min": sum(s.memory_percent for s in recent) / len(recent),
            "max_cpu_1min": max(s.cpu_percent for s in recent),
            "max_memory_1min": max(s.memory_percent for s in recent),
            "alerts_last_hour": len([a for a in self.alerts 
                                    if a.timestamp > datetime.now() - timedelta(hours=1)])
        }
    
    def export_history(self, filepath: str):
        """Export resource history to JSON"""
        data = {
            "start_time": self.start_time,
            "config": {
                "check_interval": self.check_interval,
                "thresholds": self.thresholds
            },
            "history": [
                {
                    "timestamp": s.timestamp.isoformat(),
                    "cpu_percent": s.cpu_percent,
                    "memory_percent": s.memory_percent,
                    "process_memory_mb": s.process_memory_mb
                }
                for s in self.history
            ],
            "alerts": [
                {
                    "timestamp": a.timestamp.isoformat(),
                    "level": a.level,
                    "resource": a.resource,
                    "message": a.message,
                    "value": a.value
                }
                for a in self.alerts
            ]
        }
        
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2)
        
        logger.info(f"Exported resource history to {filepath}")

class AdaptiveResourceManager:
    """
    Manages resources adaptively based on monitoring
    """
    
    def __init__(self, monitor: ResourceMonitor = None):
        self.monitor = monitor or ResourceMonitor()
        self.adaptation_history = deque(maxlen=100)
        
        # Register adaptation callback
        self.monitor.add_adaptation_callback(self._adapt_resources)
        
        # Components to manage
        self.managed_components = {}
    
    def register_component(self, name: str, component: Any):
        """Register a component for adaptive management"""
        self.managed_components[name] = component
    
    def _adapt_resources(self, snapshot: ResourceSnapshot):
        """Adapt resources based on current state"""
        adaptation = {
            "timestamp": snapshot.timestamp,
            "actions": []
        }
        
        # High CPU - reduce processing
        if snapshot.cpu_percent > 80:
            # Reduce chunk size for streaming
            if "streaming_processor" in self.managed_components:
                processor = self.managed_components["streaming_processor"]
                if hasattr(processor, 'config'):
                    processor.config.chunk_size_samples = max(512, 
                        processor.config.chunk_size_samples // 2)
                    adaptation["actions"].append("Reduced chunk size")
        
        # High memory - unload models (macOS-aware)
        available_gb = snapshot.memory_available_mb / 1024.0
        if available_gb < 1.0:  # Less than 1GB available
            if "model_manager" in self.managed_components:
                manager = self.managed_components["model_manager"]
                manager._emergency_cleanup()
                adaptation["actions"].append(f"Emergency model cleanup ({available_gb:.1f}GB available)")
        
        # Thermal throttling - reduce all activity
        if snapshot.thermal_state in ["serious", "critical"]:
            # Pause non-essential processing
            adaptation["actions"].append("Thermal throttling active")
        
        self.adaptation_history.append(adaptation)
        
        if adaptation["actions"]:
            logger.info(f"Adapted resources: {', '.join(adaptation['actions'])}")

# Example usage
def example_monitoring():
    """Example resource monitoring setup"""
    monitor = ResourceMonitor(check_interval=1.0)
    
    # Add alert handler
    def handle_alert(alert: ResourceAlert):
        print(f"ALERT [{alert.level}] {alert.resource}: {alert.message}")
    
    monitor.add_alert_callback(handle_alert)
    
    # Start monitoring
    monitor.start()
    
    # Simulate some work
    import numpy as np
    for i in range(10):
        # Do some CPU intensive work
        data = np.random.randn(1000000)
        result = np.fft.fft(data)
        
        # Get current stats
        stats = monitor.get_stats()
        if stats:
            print(f"CPU: {stats.get('average_cpu_1min', 0):.1f}%, "
                  f"Memory: {stats.get('average_memory_1min', 0):.1f}%")
        
        time.sleep(1)
    
    # Export history
    monitor.export_history("resource_history.json")
    
    # Stop
    monitor.stop()

if __name__ == "__main__":
    example_monitoring()