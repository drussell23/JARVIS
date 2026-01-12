"""
Python fallback implementation for zero-copy memory pool.
Used when Rust implementation is not available.
"""

import numpy as np
import logging
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
from datetime import datetime
import weakref
import gc

logger = logging.getLogger(__name__)

@dataclass
class PythonBuffer:
    """A buffer managed by the Python zero-copy pool."""
    data: np.ndarray
    size: int
    allocated_at: datetime
    pool_ref: weakref.ref
    
    def release(self):
        """Release buffer back to pool."""
        pool = self.pool_ref()
        if pool:
            pool.release_buffer(self)
            
    def __del__(self):
        """Ensure buffer is released when garbage collected."""
        try:
            self.release()
        except Exception:
            pass

class PythonZeroCopyPool:
    """
    Python implementation of zero-copy memory pool.
    Provides similar interface to Rust version but with Python/NumPy.
    """
    
    def __init__(self, max_memory_mb: Optional[int] = None):
        """
        Initialize the pool.
        
        Args:
            max_memory_mb: Maximum memory to use in MB (default: 40% of available)
        """
        if max_memory_mb is None:
            import psutil
            # Use 40% of available memory
            available_mb = psutil.virtual_memory().available // (1024 * 1024)
            max_memory_mb = int(available_mb * 0.4)
            
        self.max_memory_bytes = max_memory_mb * 1024 * 1024
        self.current_usage = 0
        self.free_buffers: Dict[int, List[np.ndarray]] = {}
        self.stats = {
            'allocations': 0,
            'releases': 0,
            'reuses': 0,
            'peak_usage': 0
        }
        
        logger.info(f"Python zero-copy pool initialized with {max_memory_mb}MB limit")
        
    def allocate(self, size_bytes: int) -> PythonBuffer:
        """
        Allocate a buffer of the specified size.
        
        Args:
            size_bytes: Size in bytes
            
        Returns:
            PythonBuffer instance
        """
        # Round up to nearest power of 2 for better reuse
        alloc_size = 1 << (size_bytes - 1).bit_length()
        
        # Check memory limit
        if self.current_usage + alloc_size > self.max_memory_bytes:
            self._try_gc()
            if self.current_usage + alloc_size > self.max_memory_bytes:
                raise MemoryError(f"Would exceed memory limit: {self.max_memory_bytes}")
                
        # Try to reuse existing buffer
        if alloc_size in self.free_buffers and self.free_buffers[alloc_size]:
            array = self.free_buffers[alloc_size].pop()
            self.stats['reuses'] += 1
            logger.debug(f"Reused buffer of size {alloc_size}")
        else:
            # Allocate new buffer
            array = np.zeros(alloc_size, dtype=np.uint8)
            self.current_usage += alloc_size
            self.stats['peak_usage'] = max(self.stats['peak_usage'], self.current_usage)
            
        self.stats['allocations'] += 1
        
        return PythonBuffer(
            data=array,
            size=size_bytes,  # Actual requested size
            allocated_at=datetime.now(),
            pool_ref=weakref.ref(self)
        )
        
    def release_buffer(self, buffer: PythonBuffer):
        """Release a buffer back to the pool."""
        if buffer.data is None:
            return
            
        alloc_size = len(buffer.data)
        
        # Add to free list
        if alloc_size not in self.free_buffers:
            self.free_buffers[alloc_size] = []
            
        self.free_buffers[alloc_size].append(buffer.data)
        buffer.data = None  # Clear reference
        
        self.stats['releases'] += 1
        
    def _try_gc(self):
        """Try garbage collection to free memory."""
        gc.collect()
        
        # Remove old buffers from free lists
        for size, buffers in list(self.free_buffers.items()):
            if len(buffers) > 2:  # Keep at most 2 free buffers per size
                removed = len(buffers) - 2
                self.free_buffers[size] = buffers[:2]
                self.current_usage -= size * removed
                logger.debug(f"Freed {removed} buffers of size {size}")
                
    def stats(self) -> Dict[str, Any]:
        """Get pool statistics."""
        free_count = sum(len(bufs) for bufs in self.free_buffers.values())
        free_memory = sum(size * len(bufs) for size, bufs in self.free_buffers.items())
        
        return {
            'total_allocations': self.stats['allocations'],
            'total_releases': self.stats['releases'],
            'buffer_reuses': self.stats['reuses'],
            'current_usage_mb': self.current_usage / (1024 * 1024),
            'peak_usage_mb': self.stats['peak_usage'] / (1024 * 1024),
            'max_memory_mb': self.max_memory_bytes / (1024 * 1024),
            'free_buffers': free_count,
            'free_memory_mb': free_memory / (1024 * 1024),
            'reuse_rate': self.stats['reuses'] / max(1, self.stats['allocations'])
        }
        
    def clear(self):
        """Clear all free buffers."""
        self.free_buffers.clear()
        self.current_usage = 0
        gc.collect()
        
    def __repr__(self):
        stats = self.stats()
        return (f"PythonZeroCopyPool(current={stats['current_usage_mb']:.1f}MB, "
                f"max={stats['max_memory_mb']:.1f}MB, "
                f"reuse_rate={stats['reuse_rate']:.2f})")