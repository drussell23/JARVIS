//! Memory management with efficient buffer pooling and recycling

pub mod allocator;
pub mod pool;
pub mod recycler;
pub mod advanced_pool;
pub mod zero_copy;

use std::sync::Arc;
use parking_lot::Mutex;
use crate::{Result, JarvisError};

pub use pool::{BufferPool, PooledBuffer};
pub use allocator::{AlignedAllocator, Alignment};
pub use recycler::{BufferRecycler, RecyclableBuffer};
pub use zero_copy::{ZeroCopyBuffer, ZeroCopyPool, MemoryPressure};

/// Global memory manager
static MEMORY_MANAGER: once_cell::sync::Lazy<Arc<MemoryManager>> = 
    once_cell::sync::Lazy::new(|| Arc::new(MemoryManager::new()));

/// Memory allocation statistics
#[derive(Debug, Clone, Default)]
pub struct MemoryStats {
    pub total_allocated_bytes: usize,
    pub active_allocations: usize,
    pub pool_hits: usize,
    pub pool_misses: usize,
    pub recycled_buffers: usize,
    pub peak_usage_bytes: usize,
    pub allocation_count: usize,
    pub deallocation_count: usize,
    pub allocated_bytes: usize,
    pub deallocated_bytes: usize,
}

/// Central memory manager
pub struct MemoryManager {
    stats: Arc<Mutex<MemoryStats>>,
    pools: Vec<Arc<BufferPool>>,
    recycler: Arc<BufferRecycler>,
}

impl MemoryManager {
    fn new() -> Self {
        // Create buffer pools for common sizes
        let pools = vec![
            Arc::new(BufferPool::new(1024, 100)),        // 1KB buffers
            Arc::new(BufferPool::new(4096, 50)),         // 4KB buffers
            Arc::new(BufferPool::new(16384, 25)),        // 16KB buffers
            Arc::new(BufferPool::new(65536, 10)),        // 64KB buffers
            Arc::new(BufferPool::new(262144, 5)),        // 256KB buffers
            Arc::new(BufferPool::new(1048576, 2)),       // 1MB buffers
        ];
        
        Self {
            stats: Arc::new(Mutex::new(MemoryStats::default())),
            pools,
            recycler: Arc::new(BufferRecycler::new()),
        }
    }
    
    /// Get global memory manager instance
    pub fn global() -> Arc<MemoryManager> {
        MEMORY_MANAGER.clone()
    }
    
    /// Allocate buffer from appropriate pool
    pub fn allocate(&self, size: usize) -> Result<PooledBuffer> {
        // Find smallest pool that can satisfy request
        for pool in &self.pools {
            if pool.buffer_size() >= size {
                if let Ok(buffer) = pool.acquire() {
                    let mut stats = self.stats.lock();
                    stats.pool_hits += 1;
                    stats.active_allocations += 1;
                    stats.total_allocated_bytes += pool.buffer_size();
                    return Ok(buffer);
                }
            }
        }
        
        // No suitable pool found, allocate directly
        let mut stats = self.stats.lock();
        stats.pool_misses += 1;
        stats.active_allocations += 1;
        stats.total_allocated_bytes += size;
        
        Ok(PooledBuffer::new_direct(size))
    }
    
    /// Get memory statistics
    pub fn stats(&self) -> MemoryStats {
        self.stats.lock().clone()
    }
    
    /// Get recycler for buffer reuse
    pub fn recycler(&self) -> Arc<BufferRecycler> {
        self.recycler.clone()
    }
}

// // ZeroCopyBuffer is now exported from zero_copy module
// /*
// /// Zero-copy buffer for Python interop
// #[derive(Debug)]
// pub struct ZeroCopyBuffer {
//     data: *mut u8,
//     size: usize,
//     alignment: usize,
//     owner: BufferOwner,
// }
// 
// #[derive(Debug)]
// enum BufferOwner {
//     Rust(PooledBuffer),
//     Python(*mut pyo3::ffi::PyObject),
// }
// 
// unsafe impl Send for ZeroCopyBuffer {}
// unsafe impl Sync for ZeroCopyBuffer {}
// 
// impl ZeroCopyBuffer {
//     /// Create from Rust-owned buffer
//     pub fn from_rust(mut buffer: PooledBuffer) -> Self {*/
//         let data = buffer.as_mut_ptr();
//         let size = buffer.len();
//         
//         Self {
//             data,
//             size,
//             alignment: 64,  // Default alignment
//             owner: BufferOwner::Rust(buffer),
//         }
//     }
//     
//     /// Create from Python-owned buffer (for zero-copy from NumPy)
//     #[cfg(feature = "python-bindings")]
//     pub unsafe fn from_python(obj: *mut pyo3::ffi::PyObject, data: *mut u8, size: usize) -> Self {
//         use pyo3::ffi;
//         
//         // Increment reference count to keep Python object alive
//         ffi::Py_INCREF(obj);
//         
//         Self {
//             data,
//             size,
//             alignment: 1,  // Unknown alignment from Python
//             owner: BufferOwner::Python(obj),
//         }
//     }
//     
//     /// Get data pointer
//     pub fn as_ptr(&self) -> *const u8 {
//         self.data
//     }
//     
//     /// Get mutable data pointer
//     pub fn as_mut_ptr(&self) -> *mut u8 {
//         self.data
//     }
//     
//     /// Get buffer size
//     pub fn len(&self) -> usize {
//         self.size
//     }
//     
//     /// Get as slice
//     pub unsafe fn as_slice(&self) -> &[u8] {
//         std::slice::from_raw_parts(self.data, self.size)
//     }
//     
//     /// Get as mutable slice
//     pub unsafe fn as_mut_slice(&mut self) -> &mut [u8] {
//         std::slice::from_raw_parts_mut(self.data, self.size)
//     }
// }
// 
// impl Drop for ZeroCopyBuffer {
//     fn drop(&mut self) {
//         match &self.owner {
//             BufferOwner::Rust(_) => {
//                 // Rust buffer will be returned to pool automatically
//             }
//             BufferOwner::Python(obj) => {
//                 // Decrement Python reference count
//                 #[cfg(feature = "python-bindings")]
//                 unsafe {
//                     pyo3::ffi::Py_DECREF(*obj);
//                 }
//             }
//         }
//     }
// }

#[cfg(test)]
mod tests {
    use super::*;
    
    #[test]
    fn test_memory_allocation() {
        let manager = MemoryManager::global();
        
        // Test small allocation
        let buf1 = manager.allocate(512).unwrap();
        assert!(buf1.len() >= 512);
        
        // Test medium allocation
        let buf2 = manager.allocate(8192).unwrap();
        assert!(buf2.len() >= 8192);
        
        // Check stats
        let stats = manager.stats();
        assert!(stats.pool_hits > 0);
        assert!(stats.active_allocations > 0);
    }
}