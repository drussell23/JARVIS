"""
Python fallback implementation for memory pool.
Used when Rust implementation is not available.
"""

import numpy as np
import threading
import logging
from typing import Optional, Dict, Any, List, Tuple
from collections import defaultdict
from datetime import datetime
import weakref
import psutil

logger = logging.getLogger(__name__)

class MemoryBuffer:
    """A buffer managed by the memory pool."""
    
    def __init__(self, data: np.ndarray, size: int, pool: 'PythonMemoryPool'):
        self.data = data
        self.size = size
        self.pool_ref = weakref.ref(pool)
        self.in_use = True
        
    def __enter__(self):
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        
    def release(self):
        """Release buffer back to pool."""
        if self.in_use:
            pool = self.pool_ref()
            if pool:
                pool.release_buffer(self)
            self.in_use = False
            
    def __del__(self):
        """Ensure buffer is released."""
        try:
            self.release()
        except Exception:
            pass

class PythonMemoryPool:
    """
    Python implementation of advanced memory pool.
    Provides similar interface to Rust version.
    """
    
    def __init__(self, enable_leak_detection: bool = True):
        """Initialize the memory pool."""
        self.pools: Dict[int, List[np.ndarray]] = defaultdict(list)
        self.stats = {
            'allocations': 0,
            'releases': 0,
            'hits': 0,
            'misses': 0,
            'leaked_buffers': 0,
            'total_allocated': 0,
            'total_released': 0
        }
        self.lock = threading.Lock()
        self.enable_leak_detection = enable_leak_detection
        self.active_buffers: weakref.WeakSet = weakref.WeakSet()
        
        # Configure pool sizes
        self.pool_sizes = [
            1024,           # 1KB
            4096,           # 4KB
            16384,          # 16KB
            65536,          # 64KB
            262144,         # 256KB
            1048576,        # 1MB
            4194304,        # 4MB
        ]
        
        # Pre-allocate some buffers
        self._preallocate()
        
        logger.info("Python memory pool initialized")
        
    def _preallocate(self):
        """Pre-allocate some common buffer sizes."""
        prealloc_counts = {
            1024: 10,
            4096: 5,
            16384: 3,
            65536: 2,
            262144: 1
        }
        
        with self.lock:
            for size, count in prealloc_counts.items():
                for _ in range(count):
                    buffer = np.zeros(size, dtype=np.uint8)
                    self.pools[size].append(buffer)
                    self.stats['total_allocated'] += size
                    
    def allocate(self, size: int) -> MemoryBuffer:
        """
        Allocate a buffer of at least the specified size.
        
        Args:
            size: Minimum size in bytes
            
        Returns:
            MemoryBuffer instance
        """
        # Find the smallest pool size that fits
        pool_size = size
        for ps in self.pool_sizes:
            if ps >= size:
                pool_size = ps
                break
        else:
            # Round up to next power of 2 for large sizes
            pool_size = 1 << (size - 1).bit_length()
            
        with self.lock:
            # Try to get from pool
            if pool_size in self.pools and self.pools[pool_size]:
                array = self.pools[pool_size].pop()
                self.stats['hits'] += 1
                logger.debug(f"Pool hit for size {pool_size}")
            else:
                # Allocate new
                array = np.zeros(pool_size, dtype=np.uint8)
                self.stats['misses'] += 1
                self.stats['total_allocated'] += pool_size
                logger.debug(f"Pool miss for size {pool_size}, allocated new")
                
            self.stats['allocations'] += 1
            
            # Create buffer
            buffer = MemoryBuffer(array, size, self)
            
            # Track for leak detection
            if self.enable_leak_detection:
                self.active_buffers.add(buffer)
                
            return buffer
            
    def release_buffer(self, buffer: MemoryBuffer):
        """Release a buffer back to the pool."""
        if not buffer.in_use:
            return
            
        pool_size = len(buffer.data)
        
        with self.lock:
            # Return to pool
            self.pools[pool_size].append(buffer.data)
            self.stats['releases'] += 1
            self.stats['total_released'] += pool_size
            
            # Clear the buffer data reference
            buffer.data = None
            buffer.in_use = False
            
    def clear_pool(self, size: Optional[int] = None):
        """Clear buffers from pool."""
        with self.lock:
            if size is None:
                # Clear all pools
                total_cleared = 0
                for pool_size, buffers in self.pools.items():
                    total_cleared += pool_size * len(buffers)
                    buffers.clear()
                    
                self.pools.clear()
                logger.info(f"Cleared all pools, freed {total_cleared / (1024*1024):.1f}MB")
            else:
                # Clear specific size
                if size in self.pools:
                    count = len(self.pools[size])
                    self.pools[size].clear()
                    logger.info(f"Cleared {count} buffers of size {size}")
                    
    def detect_leaks(self) -> List[Dict[str, Any]]:
        """Detect potential memory leaks."""
        if not self.enable_leak_detection:
            return []
            
        leaks = []
        
        # Check for buffers that should have been released
        for buffer in list(self.active_buffers):
            if buffer.in_use:
                leaks.append({
                    'size': buffer.size,
                    'pool_size': len(buffer.data) if buffer.data is not None else 0,
                    'id': id(buffer)
                })
                
        self.stats['leaked_buffers'] = len(leaks)
        
        if leaks:
            logger.warning(f"Detected {len(leaks)} potential memory leaks")
            
        return leaks
        
    def get_stats(self) -> Dict[str, Any]:
        """Get pool statistics."""
        with self.lock:
            pool_info = {}
            total_pooled_memory = 0
            total_pooled_buffers = 0
            
            for size, buffers in self.pools.items():
                count = len(buffers)
                if count > 0:
                    pool_info[f"pool_{size}"] = count
                    total_pooled_memory += size * count
                    total_pooled_buffers += count
                    
            stats = self.stats.copy()
            stats.update({
                'pool_info': pool_info,
                'total_pooled_buffers': total_pooled_buffers,
                'total_pooled_memory_mb': total_pooled_memory / (1024 * 1024),
                'hit_rate': stats['hits'] / max(1, stats['allocations']),
                'fragmentation': self._calculate_fragmentation(),
                'memory_efficiency': stats['total_released'] / max(1, stats['total_allocated'])
            })
            
            # Add system memory info
            mem_info = psutil.virtual_memory()
            stats['system_memory'] = {
                'total_gb': mem_info.total / (1024**3),
                'available_gb': mem_info.available / (1024**3),
                'percent_used': mem_info.percent
            }
            
            return stats
            
    def _calculate_fragmentation(self) -> float:
        """Calculate memory fragmentation score (0-1, lower is better)."""
        with self.lock:
            if not self.pools:
                return 0.0
                
            # Calculate how well distributed the pooled buffers are
            sizes = list(self.pools.keys())
            if len(sizes) <= 1:
                return 0.0
                
            # More variety in pool sizes = more fragmentation
            unique_sizes = len(sizes)
            total_buffers = sum(len(bufs) for bufs in self.pools.values())
            
            if total_buffers == 0:
                return 0.0
                
            # Average buffers per size
            avg_per_size = total_buffers / unique_sizes
            
            # Calculate variance
            variance = sum((len(self.pools[size]) - avg_per_size) ** 2 
                          for size in sizes) / unique_sizes
                          
            # Normalize to 0-1
            fragmentation = min(1.0, variance / (total_buffers ** 2))
            
            return fragmentation
            
    def optimize(self):
        """Optimize pool by removing excess buffers."""
        with self.lock:
            for size, buffers in list(self.pools.items()):
                # Keep at most 3 buffers of each size
                if len(buffers) > 3:
                    excess = len(buffers) - 3
                    self.pools[size] = buffers[:3]
                    freed = size * excess
                    logger.info(f"Optimized pool: freed {excess} buffers of size {size} "
                              f"({freed / (1024*1024):.1f}MB)")
                    
    def __repr__(self):
        stats = self.get_stats()
        return (f"PythonMemoryPool(pools={len(self.pools)}, "
                f"pooled={stats['total_pooled_memory_mb']:.1f}MB, "
                f"hit_rate={stats['hit_rate']:.2f})")