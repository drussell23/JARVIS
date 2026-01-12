"""
Memory Manager - Resource Controller for JARVIS
Real-time memory monitoring with predictive loading/unloading
"""

import asyncio
import logging
import psutil
import time
from typing import Dict, List, Optional, Callable, Any, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum
import json
from collections import deque

logger = logging.getLogger(__name__)

class MemoryPressure(Enum):
    """Memory pressure levels"""
    LOW = "low"          # < 40% - Can load anything
    MODERATE = "moderate" # 40-60% - Normal operation
    HIGH = "high"        # 60-75% - Limit operations
    CRITICAL = "critical" # > 75% - Emergency mode
    

@dataclass
class MemorySnapshot:
    """Snapshot of memory state at a point in time"""
    timestamp: datetime
    total_mb: float
    available_mb: float
    percent_used: float
    pressure: MemoryPressure
    top_processes: List[Dict[str, Any]]
    

class MemoryController:
    """Advanced memory management with predictive capabilities"""
    
    def __init__(self, 
                 target_percent: float = 60.0,
                 monitoring_interval: float = 5.0):
        self.target_percent = target_percent
        self.monitoring_interval = monitoring_interval
        
        # Thresholds
        self.thresholds = {
            MemoryPressure.LOW: 40,
            MemoryPressure.MODERATE: 60,
            MemoryPressure.HIGH: 75,
            MemoryPressure.CRITICAL: 85
        }
        
        # Memory history for predictions
        self.memory_history = deque(maxlen=100)  # Last 100 snapshots
        self.pressure_callbacks: Dict[MemoryPressure, List[Callable]] = {
            pressure: [] for pressure in MemoryPressure
        }
        
        # Monitoring state
        self.monitoring_task = None
        self.is_monitoring = False
        
        # Predictions
        self.predicted_pressure = MemoryPressure.MODERATE
        self.prediction_confidence = 0.0
        
        # Garbage collection stats
        self.gc_runs = 0
        self.gc_freed_mb = 0
        
    async def start_monitoring(self):
        """Start real-time memory monitoring"""
        if self.is_monitoring:
            return
            
        self.is_monitoring = True
        self.monitoring_task = asyncio.create_task(self._monitor_loop())
        logger.info("Memory monitoring started")
        
    async def stop_monitoring(self):
        """Stop memory monitoring"""
        self.is_monitoring = False
        if self.monitoring_task:
            self.monitoring_task.cancel()
            try:
                await self.monitoring_task
            except asyncio.CancelledError:
                pass
        logger.info("Memory monitoring stopped")
        
    async def _monitor_loop(self):
        """Main monitoring loop"""
        while self.is_monitoring:
            try:
                # Take snapshot
                snapshot = self._take_snapshot()
                self.memory_history.append(snapshot)
                
                # Check for pressure changes
                await self._check_pressure_change(snapshot)
                
                # Run predictions
                self._update_predictions()
                
                # Automatic optimization if needed
                if snapshot.pressure in [MemoryPressure.HIGH, MemoryPressure.CRITICAL]:
                    await self.optimize_memory(aggressive=snapshot.pressure == MemoryPressure.CRITICAL)
                    
            except Exception as e:
                logger.error(f"Error in memory monitoring: {e}")
                
            await asyncio.sleep(self.monitoring_interval)
            
    def _take_snapshot(self) -> MemorySnapshot:
        """Take a memory snapshot"""
        mem = psutil.virtual_memory()
        
        # Get top memory processes
        top_processes = []
        for proc in psutil.process_iter(['pid', 'name', 'memory_percent']):
            try:
                if proc.info['memory_percent'] > 1.0:
                    top_processes.append({
                        'pid': proc.info['pid'],
                        'name': proc.info['name'],
                        'memory_percent': proc.info['memory_percent']
                    })
            except Exception:
                continue
                
        top_processes.sort(key=lambda x: x['memory_percent'], reverse=True)
        
        return MemorySnapshot(
            timestamp=datetime.now(),
            total_mb=mem.total / (1024 * 1024),
            available_mb=mem.available / (1024 * 1024),
            percent_used=mem.percent,
            pressure=self._calculate_pressure(mem.percent),
            top_processes=top_processes[:5]
        )
        
    def _calculate_pressure(self, percent: float) -> MemoryPressure:
        """Calculate memory pressure from percentage"""
        if percent >= self.thresholds[MemoryPressure.CRITICAL]:
            return MemoryPressure.CRITICAL
        elif percent >= self.thresholds[MemoryPressure.HIGH]:
            return MemoryPressure.HIGH
        elif percent >= self.thresholds[MemoryPressure.MODERATE]:
            return MemoryPressure.MODERATE
        else:
            return MemoryPressure.LOW
            
    async def _check_pressure_change(self, snapshot: MemorySnapshot):
        """Check if pressure has changed and trigger callbacks"""
        if len(self.memory_history) < 2:
            return
            
        prev_pressure = self.memory_history[-2].pressure
        curr_pressure = snapshot.pressure
        
        if prev_pressure != curr_pressure:
            logger.info(f"Memory pressure changed: {prev_pressure.value} -> {curr_pressure.value}")
            
            # Trigger callbacks
            for callback in self.pressure_callbacks[curr_pressure]:
                try:
                    await callback(snapshot)
                except Exception as e:
                    logger.error(f"Error in pressure callback: {e}")
                    
    def _update_predictions(self):
        """Update memory pressure predictions"""
        if len(self.memory_history) < 10:
            return
            
        # Simple trend analysis
        recent = list(self.memory_history)[-10:]
        trend = sum(s.percent_used for s in recent[5:]) / 5 - \
                sum(s.percent_used for s in recent[:5]) / 5
                
        # Predict future pressure
        current = self.memory_history[-1]
        predicted_percent = current.percent_used + (trend * 2)  # Predict 2 intervals ahead
        
        self.predicted_pressure = self._calculate_pressure(predicted_percent)
        self.prediction_confidence = min(abs(trend) / 5, 1.0)  # Higher trend = higher confidence
        
        if self.prediction_confidence > 0.7 and self.predicted_pressure.value > current.pressure.value:
            logger.warning(f"Memory pressure predicted to increase to {self.predicted_pressure.value}")
            
    async def optimize_memory(self, aggressive: bool = False) -> Dict[str, Any]:
        """Optimize memory usage"""
        start_snapshot = self._take_snapshot()
        logger.info(f"Starting memory optimization (aggressive={aggressive})")
        
        freed_mb = 0
        actions = []
        
        # Force garbage collection
        import gc
        gc.collect()
        gc_freed = self._calculate_freed_memory(start_snapshot)
        freed_mb += gc_freed
        self.gc_runs += 1
        self.gc_freed_mb += gc_freed
        actions.append(f"Garbage collection: {gc_freed:.0f}MB")
        
        if aggressive:
            # Clear caches
            if hasattr(asyncio, '_get_running_loop'):
                loop = asyncio.get_running_loop()
                if hasattr(loop, '_ready'):
                    loop._ready.clear()
                    actions.append("Cleared asyncio ready queue")
                    
            # More aggressive GC
            gc.collect(2)  # Full collection
            additional = self._calculate_freed_memory(start_snapshot) - freed_mb
            freed_mb += additional
            actions.append(f"Full GC: {additional:.0f}MB")
            
        # Final snapshot
        end_snapshot = self._take_snapshot()
        
        result = {
            "success": end_snapshot.percent_used < start_snapshot.percent_used,
            "start_percent": start_snapshot.percent_used,
            "end_percent": end_snapshot.percent_used,
            "freed_mb": freed_mb,
            "actions": actions
        }
        
        logger.info(f"Memory optimization complete: {result}")
        return result
        
    def _calculate_freed_memory(self, start_snapshot: MemorySnapshot) -> float:
        """Calculate how much memory was freed"""
        current = self._take_snapshot()
        return current.available_mb - start_snapshot.available_mb
        
    def register_pressure_callback(self, pressure: MemoryPressure, callback: Callable):
        """Register a callback for pressure changes"""
        self.pressure_callbacks[pressure].append(callback)
        
    def get_memory_stats(self) -> Dict[str, Any]:
        """Get comprehensive memory statistics"""
        current = self._take_snapshot()
        
        # Calculate averages from history
        if self.memory_history:
            avg_percent = sum(s.percent_used for s in self.memory_history) / len(self.memory_history)
            max_percent = max(s.percent_used for s in self.memory_history)
            min_percent = min(s.percent_used for s in self.memory_history)
        else:
            avg_percent = max_percent = min_percent = current.percent_used
            
        return {
            "current": {
                "percent_used": current.percent_used,
                "available_mb": current.available_mb,
                "pressure": current.pressure.value,
                "top_processes": current.top_processes[:3]
            },
            "history": {
                "average_percent": avg_percent,
                "max_percent": max_percent,
                "min_percent": min_percent,
                "samples": len(self.memory_history)
            },
            "prediction": {
                "predicted_pressure": self.predicted_pressure.value,
                "confidence": self.prediction_confidence
            },
            "optimization": {
                "gc_runs": self.gc_runs,
                "gc_freed_mb": self.gc_freed_mb
            }
        }
        
    async def wait_for_memory(self, required_mb: float, timeout: float = 30.0) -> bool:
        """Wait for sufficient memory to become available"""
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            snapshot = self._take_snapshot()
            if snapshot.available_mb >= required_mb:
                return True
                
            # Try optimization
            if snapshot.pressure in [MemoryPressure.HIGH, MemoryPressure.CRITICAL]:
                await self.optimize_memory(aggressive=True)
                
            await asyncio.sleep(1)
            
        return False
        
    def should_load_model(self, model_size_mb: float) -> Tuple[bool, str]:
        """Check if it's safe to load a model of given size"""
        snapshot = self._take_snapshot()
        
        # Check current availability
        if snapshot.available_mb < model_size_mb + 1024:  # Keep 1GB buffer
            return False, "Insufficient memory available"
            
        # Check pressure
        if snapshot.pressure == MemoryPressure.CRITICAL:
            return False, "Memory pressure is critical"
        elif snapshot.pressure == MemoryPressure.HIGH and model_size_mb > 2048:
            return False, "Memory pressure too high for large model"
            
        # Check prediction
        if self.predicted_pressure == MemoryPressure.CRITICAL and self.prediction_confidence > 0.7:
            return False, "Memory pressure predicted to become critical"
            
        return True, "OK"