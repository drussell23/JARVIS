//! Zero-copy memory management for high-performance vision processing
//! Optimized for 16GB RAM systems with dynamic allocation

use std::sync::{Arc, Weak};
use std::sync::atomic::{AtomicUsize, AtomicBool, Ordering};
use parking_lot::{RwLock, Mutex};
use std::collections::HashMap;
use std::ptr::NonNull;
use std::alloc::{alloc, dealloc, Layout};
use std::time::{Instant, Duration};
use crossbeam::channel::{bounded, Sender, Receiver};
use std::thread;

/// Memory pressure levels for dynamic adjustment
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MemoryPressure {
    Low,      // < 40% usage
    Normal,   // 40-60% usage
    High,     // 60-80% usage
    Critical, // > 80% usage
}

/// Thread-safe zero-copy buffer with reference counting
pub struct ZeroCopyBuffer {
    inner: Arc<ZeroCopyBufferInner>,
    pool: Weak<ZeroCopyPool>,
}

struct ZeroCopyBufferInner {
    ptr: NonNull<u8>,
    size: usize,
    layout: Layout,
    ref_count: AtomicUsize,
    allocated_at: Instant,
}

// SAFETY:
// - `ptr` points to uniquely owned heap memory for this allocation.
// - shared ownership is via `Arc`, and destruction is coordinated by atomics.
// - no thread may produce mutable references unless `Arc::get_mut` proves uniqueness.
unsafe impl Send for ZeroCopyBufferInner {}
// SAFETY: same invariants as `Send` above apply for cross-thread shared access.
unsafe impl Sync for ZeroCopyBufferInner {}

impl ZeroCopyBuffer {
    /// Create new zero-copy buffer
    fn new(size: usize, pool: Weak<ZeroCopyPool>) -> Result<Self, String> {
        let layout = Layout::from_size_align(size, 64) // 64-byte alignment for SIMD
            .map_err(|e| format!("Invalid layout: {}", e))?;
            
        let ptr = unsafe {
            let ptr = alloc(layout);
            if ptr.is_null() {
                return Err("Failed to allocate memory".to_string());
            }
            NonNull::new_unchecked(ptr)
        };
        
        Ok(Self {
            inner: Arc::new(ZeroCopyBufferInner {
                ptr,
                size,
                layout,
                ref_count: AtomicUsize::new(1),
                allocated_at: Instant::now(),
            }),
            pool,
        })
    }
    
    /// Get buffer as slice
    pub fn as_slice(&self) -> &[u8] {
        unsafe {
            std::slice::from_raw_parts(self.inner.ptr.as_ptr(), self.inner.size)
        }
    }
    
    /// Get buffer as mutable slice when this handle is the unique owner.
    /// Returns `None` if cloned references exist.
    pub fn as_mut_slice(&mut self) -> Option<&mut [u8]> {
        let inner = Arc::get_mut(&mut self.inner)?;
        Some(unsafe {
            std::slice::from_raw_parts_mut(inner.ptr.as_ptr(), inner.size)
        })
    }
    
    /// Get raw pointer for zero-copy operations
    pub fn as_ptr(&self) -> *const u8 {
        self.inner.ptr.as_ptr()
    }
    
    /// Get mutable raw pointer when this handle is the unique owner.
    /// Returns `None` if cloned references exist.
    pub fn as_mut_ptr(&mut self) -> Option<*mut u8> {
        let inner = Arc::get_mut(&mut self.inner)?;
        Some(inner.ptr.as_ptr())
    }
    
    /// Clone with reference counting
    pub fn clone_ref(&self) -> Self {
        self.inner.ref_count.fetch_add(1, Ordering::SeqCst);
        Self {
            inner: self.inner.clone(),
            pool: self.pool.clone(),
        }
    }
    
    /// Get age of buffer
    pub fn age(&self) -> Duration {
        self.inner.allocated_at.elapsed()
    }

    /// Get size of buffer
    pub fn size(&self) -> usize {
        self.inner.size
    }
}

impl Clone for ZeroCopyBuffer {
    fn clone(&self) -> Self {
        self.clone_ref()
    }
}

impl std::fmt::Debug for ZeroCopyBuffer {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ZeroCopyBuffer")
            .field("size", &self.inner.size)
            .field("age", &self.inner.allocated_at.elapsed())
            .finish()
    }
}

impl Drop for ZeroCopyBuffer {
    fn drop(&mut self) {
        let count = self.inner.ref_count.fetch_sub(1, Ordering::SeqCst);
        
        if count == 1 {
            // Last reference, return to pool or deallocate
            if let Some(pool) = self.pool.upgrade() {
                pool.return_buffer(self.inner.ptr, self.inner.size, self.inner.layout);
            } else {
                // Pool is gone, deallocate
                unsafe {
                    dealloc(self.inner.ptr.as_ptr(), self.inner.layout);
                }
            }
        }
    }
}

/// Internal memory block structure (not exposed publicly)
struct MemoryBlock {
    ptr: NonNull<u8>,
    layout: Layout,
}

// MemoryBlock is safe to Send/Sync because:
// 1. The memory is owned exclusively by this block
// 2. All access is synchronized through RwLock in ZeroCopyPool
// 3. The raw pointer points to heap-allocated memory that won't be freed
//    until the block is consumed
// SAFETY: `MemoryBlock` has ownership-only semantics and does not provide aliasing APIs.
unsafe impl Send for MemoryBlock {}
// SAFETY: shared access is synchronized by pool locks.
unsafe impl Sync for MemoryBlock {}

/// Zero-copy memory pool with dynamic sizing
pub struct ZeroCopyPool {
    // Segregated free lists by size class
    free_lists: RwLock<HashMap<usize, Vec<MemoryBlock>>>,
    
    // Memory statistics
    total_allocated: AtomicUsize,
    total_in_use: AtomicUsize,
    allocation_count: AtomicUsize,
    
    // Configuration
    max_memory_mb: usize,
    enable_defrag: AtomicBool,
    
    // Background thread for maintenance; populated after Arc<Self> construction.
    maintenance_handle: Mutex<Option<thread::JoinHandle<()>>>,
    shutdown: Arc<AtomicBool>,
}

impl ZeroCopyPool {
    /// Create new pool with dynamic configuration
    pub fn new(max_memory_mb: usize) -> Arc<Self> {
        let shutdown = Arc::new(AtomicBool::new(false));
        let pool = Arc::new(Self {
            free_lists: RwLock::new(HashMap::new()),
            total_allocated: AtomicUsize::new(0),
            total_in_use: AtomicUsize::new(0),
            allocation_count: AtomicUsize::new(0),
            max_memory_mb,
            enable_defrag: AtomicBool::new(true),
            maintenance_handle: Mutex::new(None),
            shutdown: shutdown.clone(),
        });
        
        // Start maintenance thread
        let pool_weak = Arc::downgrade(&pool);
        let shutdown_clone = shutdown.clone();
        let maintenance_thread = thread::spawn(move || {
            Self::maintenance_loop(pool_weak, shutdown_clone);
        });
        
        // Store thread handle for deterministic shutdown.
        *pool.maintenance_handle.lock() = Some(maintenance_thread);
        
        pool
    }
    
    /// Allocate buffer from pool
    pub fn allocate(self: &Arc<Self>, size: usize) -> Result<ZeroCopyBuffer, String> {
        // Check memory pressure
        let pressure = self.get_memory_pressure();
        if pressure == MemoryPressure::Critical {
            return Err("Memory pressure too high".to_string());
        }
        
        // Round up to size class
        let size_class = Self::size_to_class(size);
        
        // Try to get from free list
        {
            let mut free_lists = self.free_lists.write();
            if let Some(list) = free_lists.get_mut(&size_class) {
                if let Some(block) = list.pop() {
                    self.total_in_use.fetch_add(size_class, Ordering::SeqCst);
                    self.allocation_count.fetch_add(1, Ordering::SeqCst);
                    
                    return Ok(ZeroCopyBuffer {
                        inner: Arc::new(ZeroCopyBufferInner {
                            ptr: block.ptr,
                            size: size_class,
                            layout: block.layout,
                            ref_count: AtomicUsize::new(1),
                            allocated_at: Instant::now(),
                        }),
                        pool: Arc::downgrade(self),
                    });
                }
            }
        }
        
        // Allocate new buffer
        self.allocate_new(size_class)
    }
    
    /// Allocate new buffer
    fn allocate_new(self: &Arc<Self>, size: usize) -> Result<ZeroCopyBuffer, String> {
        // Check if we can allocate more
        let current = self.total_allocated.load(Ordering::SeqCst);
        let max_bytes = self.max_memory_mb * 1024 * 1024;
        
        if current + size > max_bytes {
            // Try to reclaim memory first
            self.reclaim_memory();
            
            let current = self.total_allocated.load(Ordering::SeqCst);
            if current + size > max_bytes {
                return Err("Memory pool exhausted".to_string());
            }
        }
        
        // Allocate
        let buffer = ZeroCopyBuffer::new(size, Arc::downgrade(self))?;
        
        self.total_allocated.fetch_add(size, Ordering::SeqCst);
        self.total_in_use.fetch_add(size, Ordering::SeqCst);
        self.allocation_count.fetch_add(1, Ordering::SeqCst);
        
        Ok(buffer)
    }
    
    /// Return buffer to pool
    fn return_buffer(&self, ptr: NonNull<u8>, size: usize, layout: Layout) {
        let size_class = Self::size_to_class(size);
        
        // Return to free list
        let mut free_lists = self.free_lists.write();
        free_lists.entry(size_class)
            .or_insert_with(Vec::new)
            .push(MemoryBlock { ptr, layout });
            
        self.total_in_use.fetch_sub(size_class, Ordering::SeqCst);
    }
    
    /// Get memory pressure level
    pub fn get_memory_pressure(&self) -> MemoryPressure {
        let used = self.total_in_use.load(Ordering::SeqCst);
        let total = self.max_memory_mb * 1024 * 1024;
        let usage_percent = (used as f64 / total as f64) * 100.0;
        
        match usage_percent as u32 {
            0..=40 => MemoryPressure::Low,
            41..=60 => MemoryPressure::Normal,
            61..=80 => MemoryPressure::High,
            _ => MemoryPressure::Critical,
        }
    }
    
    /// Reclaim unused memory
    fn reclaim_memory(&self) {
        let mut reclaimed = 0;
        let mut free_lists = self.free_lists.write();
        
        // Remove unused buffers from free lists and deallocate backing memory.
        for (size_class, list) in free_lists.iter_mut() {
            for block in list.drain(..) {
                unsafe {
                    dealloc(block.ptr.as_ptr(), block.layout);
                }
                reclaimed += *size_class;
            }
        }
        
        self.total_allocated.fetch_sub(reclaimed, Ordering::SeqCst);
    }
    
    /// Size class rounding for better reuse
    fn size_to_class(size: usize) -> usize {
        // Round up to power of 2 or common sizes
        match size {
            0..=1024 => 1024,                    // 1KB
            1025..=4096 => 4096,                  // 4KB
            4097..=16384 => 16384,                // 16KB
            16385..=65536 => 65536,               // 64KB
            65537..=262144 => 262144,             // 256KB
            262145..=1048576 => 1048576,          // 1MB
            1048577..=4194304 => 4194304,         // 4MB
            _ => ((size + 1048575) / 1048576) * 1048576, // Round up to MB
        }
    }
    
    /// Maintenance loop
    fn maintenance_loop(pool: Weak<Self>, shutdown: Arc<AtomicBool>) {
        while !shutdown.load(Ordering::SeqCst) {
            thread::sleep(Duration::from_secs(10));
            
            if let Some(pool) = pool.upgrade() {
                // Reclaim memory if pressure is high
                let pressure = pool.get_memory_pressure();
                if pressure == MemoryPressure::High || pressure == MemoryPressure::Critical {
                    pool.reclaim_memory();
                }
                
                // Defragmentation if enabled
                if pool.enable_defrag.load(Ordering::SeqCst) {
                    // Would implement defrag here
                }
            } else {
                // Pool is gone
                break;
            }
        }
    }
    
    /// Get pool statistics
    pub fn stats(&self) -> PoolStats {
        PoolStats {
            total_allocated_bytes: self.total_allocated.load(Ordering::SeqCst),
            total_in_use_bytes: self.total_in_use.load(Ordering::SeqCst),
            allocation_count: self.allocation_count.load(Ordering::SeqCst),
            memory_pressure: self.get_memory_pressure(),
            free_list_sizes: self.free_lists.read().iter()
                .map(|(k, v)| (*k, v.len()))
                .collect(),
        }
    }
}

impl Drop for ZeroCopyPool {
    fn drop(&mut self) {
        // Signal shutdown
        self.shutdown.store(true, Ordering::SeqCst);
        
        // Join maintenance thread first so it cannot race with free-list teardown.
        if let Some(thread) = self.maintenance_handle.lock().take() {
            let _ = thread.join();
        }
        
        // Deallocate all buffers only after maintenance has stopped.
        let mut free_lists = self.free_lists.write();
        for list in free_lists.values_mut() {
            for block in list.drain(..) {
                unsafe {
                    dealloc(block.ptr.as_ptr(), block.layout);
                }
            }
        }
        free_lists.clear();
    }
}

#[derive(Debug)]
pub struct PoolStats {
    pub total_allocated_bytes: usize,
    pub total_in_use_bytes: usize,
    pub allocation_count: usize,
    pub memory_pressure: MemoryPressure,
    pub free_list_sizes: Vec<(usize, usize)>,
}

// Python bindings
#[cfg(feature = "python-bindings")]
mod python_bindings {
    use super::*;
    use pyo3::prelude::*;
    use pyo3::types::PyBytes;
    use numpy::PyArray1;
    
    #[pyclass]
    pub struct PyZeroCopyBuffer {
        buffer: ZeroCopyBuffer,
    }
    
    #[pymethods]
    impl PyZeroCopyBuffer {
        fn as_numpy(&self, py: Python<'_>) -> PyResult<Py<PyArray1<u8>>> {
            let slice = self.buffer.as_slice();
            let array = PyArray1::from_slice(py, slice);
            Ok(array.to_owned())
        }
        
        fn size(&self) -> usize {
            self.buffer.size()
        }
        
        fn age_seconds(&self) -> f64 {
            self.buffer.age().as_secs_f64()
        }
    }
    
    #[pyclass]
    pub struct PyZeroCopyPool {
        pool: Arc<ZeroCopyPool>,
    }
    
    #[pymethods]
    impl PyZeroCopyPool {
        #[new]
        #[pyo3(signature = (max_memory_mb=1024))]
        fn new(max_memory_mb: usize) -> Self {
            Self {
                pool: ZeroCopyPool::new(max_memory_mb),
            }
        }
        
        fn allocate(&self, size: usize) -> PyResult<PyZeroCopyBuffer> {
            self.pool.allocate(size)
                .map(|buffer| PyZeroCopyBuffer { buffer })
                .map_err(|e| pyo3::exceptions::PyMemoryError::new_err(e))
        }
        
        fn stats(&self) -> PyResult<Vec<(String, usize)>> {
            let stats = self.pool.stats();
            Ok(vec![
                ("total_allocated".to_string(), stats.total_allocated_bytes),
                ("total_in_use".to_string(), stats.total_in_use_bytes),
                ("allocation_count".to_string(), stats.allocation_count),
            ])
        }
        
        fn memory_pressure(&self) -> String {
            format!("{:?}", self.pool.get_memory_pressure())
        }
    }
    
    pub fn register_module(parent: &PyModule) -> PyResult<()> {
        let m = PyModule::new(parent.py(), "zero_copy")?;
        m.add_class::<PyZeroCopyBuffer>()?;
        m.add_class::<PyZeroCopyPool>()?;
        parent.add_submodule(m)?;
        Ok(())
    }
}

#[cfg(feature = "python-bindings")]
pub use python_bindings::register_module;
